from immunova.data.fcs_experiments import FCSExperiment
from immunova.data.fcs import Normalisation
from immunova.flow.gating.actions import Gating
from immunova.flow.normalisation.MMDResNet import MMDNet
from immunova.flow.supervised.ref import calculate_reference_sample
from immunova.flow.supervised.utilities import scaler
from immunova.flow.utilities import progress_bar, kde_multivariant, hellinger_dot
from immunova.flow.dim_reduction import dimensionality_reduction
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np
import math
np.random.seed(42)


class CalibrationError(Exception):
    pass


class Normalise:
    """
    Class for normalising a flow cytometry file using a reference target file
    """
    def __init__(self, experiment: FCSExperiment, source_id: str, root_population: str,
                 features: list, reference_sample: str or None = None, transform: str = 'logicle',
                 **mmdresnet_kwargs):
        """
        Constructor for Normalise object
        :param experiment: FCSExperiment object
        :param source_id: sample ID for the file to normalise
        :param reference_sample: sample ID to use as target distribution (leave as 'None' if unknown and use the
        `calculate_reference_sample` method to find an optimal reference sample)
        :param transform: transformation to apply to raw FCS data (default = 'logicle')
        :param mmdresnet_kwargs: keyword arguments for MMD-ResNet
        """
        self.experiment = experiment
        self.source_id = source_id
        self.root_population = root_population
        self.transform = transform
        self.features = [c for c in features if c.lower() != 'time']
        self.calibrator = MMDNet(data_dim=len(self.features), **mmdresnet_kwargs)
        self.reference_sample = reference_sample or None

        if source_id not in self.experiment.list_samples():
            raise CalibrationError(f'Error: invalid target sample {source_id}; '
                                   f'must be one of {self.experiment.list_samples()}')
        else:
            self.source = self.__load_and_transform(sample_id=source_id)

    def load_model(self, model_path: str) -> None:
        """
        Load an existing MMD-ResNet model from .h5 file
        :param model_path: path to model .h5 file
        :return: None
        """
        self.calibrator.load_model(path=model_path)

    def calculate_reference_sample(self) -> None:
        """
        Calculate the optimal reference sample. This is performed as described in Li et al paper
        (https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5860171/) on DeepCyTOF: for every 2 samples i, j compute
        the Frobenius norm of the difference between their covariance matrics and then select the sample
         with the smallest average distance to all other samples. Optimal sample assigned to self.reference_sample.
        :return: None
        """
        self.reference_sample = calculate_reference_sample(self.experiment)
        print(f'{self.reference_sample} chosen as optimal reference sample.')

    def __load_and_transform(self, sample_id) -> pd.DataFrame:
        """
        Given a sample ID, retrieve the sample data and apply transformation
        :param sample_id: ID corresponding to sample for retrieval
        :return: transformed data as a list of dictionary objects:
        {id: file id, typ: type of file (either 'complete' or 'control'), data: Pandas DataFrame}
        """
        gating = Gating(experiment=self.experiment, sample_id=sample_id)
        data = gating.get_population_df(self.root_population,
                                        transform=True,
                                        transform_method=self.transform,
                                        transform_features=self.features)
        if data is None:
            raise CalibrationError(f'Error: unable to load data for population {self.root_population}')
        return data[self.features]

    def __put_norm_data(self, file_id: str, data: pd.DataFrame):
        """
        Given a file ID and a Pandas DataFrame, fetch the corresponding File document and insert the normalised data.
        :param file_id: ID for file for insert
        :param data: Pandas DataFrame of normalised and transformed data
        :return:
        """
        source_fg = self.experiment.pull_sample(self.source_id)
        file = [f for f in source_fg.files if f.file_id == file_id][0]
        norm = Normalisation()
        norm.put(data.values, root_population=self.root_population, method='MMD-ResNet')
        file.norm = norm
        source_fg.save()

    def normalise_and_save(self) -> None:
        """
        Apply normalisation to source sample and save result to the database.
        :return:
        """
        if self.calibrator.model is None:
            print('Error: normalisation model has not yet been calibrated')
            return None
        print(f'Saving normalised data for {self.source_id} population {self.root_population}')
        data = self.calibrator.model.predict(self.source)
        data = pd.DataFrame(data, columns=self.source.columns)
        self.__put_norm_data(self.source_id, data)
        print('Save complete!')

    def calibrate(self, initial_lr=1e-3, lr_decay=0.97, evaluate=False, save=False) -> None:
        """
        Train the MMD-ResNet to minimise the Maximum Mean Discrepancy between our target and source sample.
        :param initial_lr: initial learning rate (default = 1e-3)
        :param lr_decay: decay rate for learning rate (default = 0.97)
        :param evaluate: If True, the performance of the training is evaluated and a PCA plot of aligned distributions
        is generated (default = False).
        :param save: If True, normalisation is applied to source sample and saved to database.
        :return: None
        """
        if self.reference_sample is None:
            print('Error: must provide a reference sample for training. This can be provided during initialisation, '
                  'by assigning a valid value to self.reference_sample, or by calling `calculate_reference_sample`.')
            return
        if self.reference_sample not in self.experiment.list_samples():
            print(f'Error: invalid reference sample {self.reference_sample}; must be one of '
                  f'{self.experiment.list_samples()}')
            return
        # Load and transform data
        target = self.__load_and_transform(self.reference_sample)
        print('Warning: calibration can take some time and is dependent on the sample size')
        self.calibrator.fit(self.source, target, initial_lr, lr_decay, evaluate=evaluate)
        print('Calibration complete!')
        if save:
            self.normalise_and_save()


class EvaluateBatchEffects:
    def __init__(self, experiment: FCSExperiment):
        self.experiment = experiment

    def _load_and_transform(self, sample_id: str, root_population: str, transform: str,
                            scale: str or None = None) -> pd.DataFrame:
        """
        Given a sample ID, retrieve the sample data and apply transformation
        :param sample_id: ID corresponding to sample for retrieval
        :return: transformed data as a list of dictionary objects:
        {id: file id, typ: type of file (either 'complete' or 'control'), data: Pandas DataFrame}
        """
        gating = Gating(experiment=self.experiment, sample_id=sample_id, include_controls=False)
        data = gating.get_population_df(root_population,
                                        transform=True,
                                        transform_method=transform,
                                        transform_features='all')
        if scale is not None:
            data = scaler(data, scale_method=scale)[0]
        if data is None:
            raise CalibrationError(f'Error: unable to load data for population {root_population}')
        return data

    def marker_variance(self, reference_id, root_population,
                        markers: list, comparison_samples: list,
                        transform: str = 'logicle',
                        scale: str or None = None,
                        figsize: tuple = (10, 10)):
        fig = plt.figure(figsize=figsize)
        nrows = math.ceil(len(markers)/3)
        exp_samples = self.experiment.list_samples()
        assert all([x in exp_samples for x in comparison_samples]), 'Invalid sample IDs provided'
        print('Fetching data...')
        reference = self._load_and_transform(reference_id, root_population, transform, scale)
        samples = [self._load_and_transform(s, root_population, transform, scale) for s in comparison_samples]
        samples = list(map(lambda s: s.sample(1000) if s.shape[0] > 1000 else s, samples))
        print('Plotting...')
        i = 0
        for marker in progress_bar(markers):
            i += 1
            if marker not in reference.columns:
                print(f'{marker} absent from reference sample, skipping')
            ax = fig.add_subplot(nrows, 3, i)
            ax = sns.kdeplot(reference[marker], shade=True, color="r", ax=ax)
            ax.set_title(f'Total variance in {marker}')
            for d in samples:
                if marker not in d.columns:
                    continue
                ax = sns.kdeplot(d[marker], color='b', shade=False, alpha=0.5, ax=ax)
                ax.get_legend().remove()
        fig.show()

    def dim_reduction_grid(self, reference_id, root_population, comparison_samples: list, features: list, sample_n=1000,
                           transform: str = 'logicle', scale: str or None = None, figsize: tuple = (10, 10),
                           method: str = 'PCA', kde: bool = False):
        fig = plt.figure(figsize=figsize)
        nrows = math.ceil(len(comparison_samples)/3)
        exp_samples = self.experiment.list_samples()
        assert all([x in exp_samples for x in comparison_samples]), 'Invalid sample IDs provided'
        print('Fetching data...')
        reference = self._load_and_transform(reference_id, root_population, transform, scale)
        samples = [self._load_and_transform(s, root_population, transform, scale) for s in comparison_samples]
        samples = list(map(lambda s: s.sample(1000) if s.shape[0] > 1000 else s, samples))
        print('Plotting...')
        reference = reference.sample(sample_n)
        reference['label'] = 'Target'
        reference, reducer = dimensionality_reduction(reference,
                                                      features=features,
                                                      method=method,
                                                      n_components=2,
                                                      return_reducer=True)
        i = 0
        for s in progress_bar(samples):
            i += 1
            s['label'] = 'Comparison'
            ax = fig.add_subplot(nrows, 3, i)
            embeddings = reducer.transform(s[features].sample(sample_n))
            x = f'{method}_0'
            y = f'{method}_1'
            ax.scatter(reference[x], reference[y], c='blue', s=4, alpha=0.2)
            if kde:
                sns.kdeplot(reference[x], reference[y], c='blue', n_levels=100, ax=ax, shade=False)
            ax.scatter(embeddings[:, 0], embeddings[:, 1], c='red', s=4, alpha=0.1)
            if kde:
                sns.kdeplot(embeddings[:, 0], embeddings[:, 1], c='red', n_levels=100, ax=ax, shade=False)
        fig.show()

    def plot_hellinger(self, target_id: str, root_population: str,
                       sample_n: int = 10000, figsize: tuple = (8, 8)):
        print('Fetching data...')
        target = self._load_and_transform(target_id, root_population, transform='logicle', scale=None)
        if target.shape[0] < sample_n:
            print('Target sample size smaller than requested sample size, continuing with entire sample')
            target = target.values
        else:
            target = target.sample(sample_n).values
        samples = list()
        for s in progress_bar(self.experiment.list_samples()):
            try:
                sample = self._load_and_transform(s, root_population, transform='logicle', scale=None)
                if sample_n < sample.shape[0]:
                    sample = sample.sample(sample_n)
                samples.append((s, sample.values))
            except CalibrationError:
                continue

        print('Calculating PDF for target...')
        x_grid = np.array([np.linspace(np.amin(target), np.amax(target), 1000) for x in range(target.shape[1])]).T
        p = kde_multivariant(target, x_grid, bandwidth='cross_val')
        print('Calculate PDF for all other samples and calculate hellinger distance...')
        fig, ax = plt.subplots(figsize=figsize)
        hd = list()
        for name, d in progress_bar(samples):
            x_grid = np.array([np.linspace(np.amin(d), np.amax(d), 1000) for x in range(d.shape[1])]).T
            q = kde_multivariant(d, x_grid, bandwidth='cross_val')
            hd.append((name, hellinger_dot(p, q)))
        print('Plotting...')
        hd_ = {'sample_id': list(), 'hellinger_distance': list()}
        for n, h in hd:
            hd_['sample_id'].append(n)
            hd_['hellinger_distance'].append(h)
        hd = pd.DataFrame(hd_).sort_values(by='hellinger_distance', ascending=True)
        sns.set_color_codes("pastel")
        ax = sns.barplot(y='sample_id', x='hellinger_distance', data=hd, color='b', ax=ax)
        ax.set_xlabel('Sample ID')
        ax.set_ylabel('Hellinger Distance')
        fig.show()


