from ..data.fcs_experiments import FCSExperiment
from .utilities import kde_multivariant, hellinger_dot, ordered_load_transform
from .feedback import progress_bar
from .dim_reduction import dimensionality_reduction
from multiprocessing import Pool, cpu_count
from functools import partial
from scipy.stats import entropy as kl
from scipy.spatial.distance import jensenshannon as jsd
from scipy.cluster import hierarchy
from scipy.spatial import distance
from collections import defaultdict
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np
import math
np.random.seed(42)


def jsd_divergence(x, y):
    div = jsd(x, y)
    assert div is not None, 'JSD is null'
    if div in [np.inf, -np.inf]:
        return 1
    return div


def kl_divergence(x, y):
    div = kl(x, y)
    assert div is not None, 'KL divergence is Null'
    return div


def indexed_kde(named_x: tuple, kde_f: callable):
    q = kde_f(named_x[1])
    return named_x[0], q


class EvaluateBatchEffects:
    def __init__(self, experiment: FCSExperiment, root_population: str, samples: list or None = None,
                 transform: str = 'logicle', scale: str or None = None,
                 sample_n: str or None = 10000):
        self.experiment = experiment
        self.sample_ids = samples or experiment.list_samples()
        self.transform = transform
        self.scale = scale
        self.sample_n = sample_n
        self.root_population = root_population
        self.data = self.load_data()
        self.kde_cache = dict()

    def load_data(self) -> dict:
        """
        Load new dataset from given FCS Experiment
        :return:
        """
        self.kde_cache = dict()
        lt = partial(ordered_load_transform,
                     experiment=self.experiment,
                     root_population=self.root_population,
                     transform=self.transform,
                     scale=self.scale,
                     sample_n=self.sample_n)
        pool = Pool(cpu_count())
        samples_df = pool.map(lt, self.sample_ids)
        samples = dict()
        for sample_id, df in samples_df:
            if df is not None:
                samples[sample_id] = df

        pool.close()
        pool.join()
        return samples

    def marker_variance(self, reference_id: str, comparison_samples: list, markers: list or None = None,
                        figsize: tuple = (10, 10)):
        """
        For a given reference sample and a list of markers of interest, create a grid of KDE plots with the reference
        sample given in red and comparison samples given in blue.
        :param reference_id: this sample will appear in red on KDE plots
        :param markers: list of valid marker names to plot in KDE grid
        :param comparison_samples: list of valid sample names in the current experiment
        :param figsize: size of resulting figure
        :return: Matplotlib figure
        """
        fig = plt.figure(figsize=figsize)
        assert reference_id in self.sample_ids, 'Invalid reference ID for experiment currently loaded'
        assert all([x in self.sample_ids for x in comparison_samples]), 'Invalid sample IDs for experiment currently ' \
                                                                        'loaded'
        reference = self.data[reference_id]
        print('Plotting...')
        i = 0
        if markers is None:
            markers = reference.columns.tolist()
        nrows = math.ceil(len(markers) / 3)
        fig.suptitle(f'Per-channel KDE, Reference: {reference_id}', y=1.05)
        for marker in progress_bar(markers):
            i += 1
            if marker not in reference.columns:
                print(f'{marker} absent from reference sample, skipping')
            ax = fig.add_subplot(nrows, 3, i)
            ax = sns.kdeplot(reference[marker], shade=True, color="b", ax=ax)
            ax.set_title(f'Total variance in {marker}')
            ax.set_xlim((0, max(reference[marker])))
            for d in comparison_samples:
                d = self.data[d]
                if marker not in d.columns:
                    continue
                ax = sns.kdeplot(d[marker], color='r', shade=False, alpha=0.5, ax=ax)
                ax.get_legend().remove()
            ax.set(aspect='auto')
        fig.tight_layout()
        fig.show()

    def dim_reduction_grid(self, reference_id, comparison_samples: list, features: list, figsize: tuple = (10, 10),
                           method: str = 'PCA', kde: bool = False):
        """
        Generate a grid of embeddings using a valid dimensionality reduction technique, in each plot a reference sample
        is shown in blue and a comparison sample in red. The reference sample is conserved across all plots.
        :param reference_id:
        :param comparison_samples:
        :param features:
        :param figsize:
        :param method:
        :param kde:
        :return:
        """
        fig = plt.figure(figsize=figsize)
        nrows = math.ceil(len(comparison_samples)/3)
        assert reference_id in self.sample_ids, 'Invalid reference ID for experiment currently loaded'
        assert all([x in self.sample_ids for x in comparison_samples]), 'Invalid sample IDs for experiment currently ' \
                                                                        'loaded'
        print('Plotting...')
        reference = self.data[reference_id]
        reference['label'] = 'Target'
        assert all([f in reference.columns for f in features]), f'Invalid features, must be in: {reference.columns}'
        reference, reducer = dimensionality_reduction(reference,
                                                      features=features,
                                                      method=method,
                                                      n_components=2,
                                                      return_reducer=True)
        i = 0
        fig.suptitle(f'{method}, Reference: {reference_id}', y=1.05)
        for s in progress_bar(comparison_samples):
            i += 1
            df = self.data[s]
            df['label'] = 'Comparison'
            ax = fig.add_subplot(nrows, 3, i)
            if not all([f in df.columns for f in features]):
                print(f'Features missing from {s}, skipping')
                continue
            embeddings = reducer.transform(df[features])
            x = f'{method}_0'
            y = f'{method}_1'
            ax.scatter(reference[x], reference[y], c='blue', s=4, alpha=0.2)
            if kde:
                sns.kdeplot(reference[x], reference[y], c='blue', n_levels=100, ax=ax, shade=False)
            ax.scatter(embeddings[:, 0], embeddings[:, 1], c='red', s=4, alpha=0.1)
            if kde:
                sns.kdeplot(embeddings[:, 0], embeddings[:, 1], c='red', n_levels=100, ax=ax, shade=False)
            ax.set_title(s)
            ax.set_yticklabels([])
            ax.set_xticklabels([])
            ax.set(aspect='auto')
        fig.tight_layout()
        fig.show()

    def divergence_barplot(self, target_id: str, comparisons: list,
                           figsize: tuple = (8, 8),kde_kernel: str = 'gaussian',
                           divergence_method: str = 'hellinger',
                           verbose: bool = False,
                           **kwargs):
        divergence = self.calc_divergence(target_id=target_id,
                                          kde_kernel=kde_kernel,
                                          divergence_method=divergence_method,
                                          verbose=verbose,
                                          comparisons=comparisons)
        # Plotting
        fig, ax = plt.subplots(figsize=figsize)
        if verbose:
            print('Plotting...')
        hd_ = {'sample_id': list(), 'hellinger_distance': list()}
        for n, h in divergence:
            hd_['sample_id'].append(n)
            hd_[f'{divergence_method} distance'].append(h)
        hd_ = pd.DataFrame(hd_).sort_values(by=f'{divergence_method} distance', ascending=True)
        sns.set_color_codes("pastel")
        ax = sns.barplot(y='sample_id', x=f'{divergence_method} distance',
                         data=hd_, color='b', ax=ax, **kwargs)
        ax.set_xlabel(f'{divergence_method} distance')
        ax.set_ylabel('Sample ID')
        fig.show()

    def divergence_matrix(self, exclude: list or None = None, figsize: tuple = (12, 12),
                          kde_kernel: str = 'gaussian', divergence_method: str = 'jsd',
                          clustering_method: str = 'average', **kwargs):
        """
        Generate a clustered heatmap of pairwise statistical distance comparisons. This can be used to find
        samples of high similarity and conversely demonstrates samples that greatly differ.
        Returns a linkage matrix that can be given to scipy.cluster.hierarchy.cut_tree to subset samples.
        Also returns Seaborn ClusterGrid instance.
        :param exclude: list of sample IDs to be omitted from plot
        :param figsize: size of resulting Seaborn clusterplot figure
        :param kde_kernel: name of kernel to use for density estimation (default = 'gaussian')
        :param divergence_method: name of statistical distance metric to use; valid choices are:
            *jsd: squared Jensen-Shannon Divergence (default)
            *kl: Kullback–Leibler divergence (relative entropy); warning, asymmetrical
            *hellinger: squared Hellinger Divergence
        :param clustering_method: method for hierarchical clustering, see scipy.cluster.hierarchy.linkage for details
        :param kwargs: additional keyword arguments to be passed to Seaborn ClusterPlot
        (seaborn.pydata.org/generated/seaborn.clustermap.html#seaborn.clustermap)
        :return: (hierarchical clustering encoded as a linkage matrix, array of sample IDs, Seaborn ClusterGrid instance)
        """
        samples = self.sample_ids
        if exclude is not None:
            samples = [s for s in samples if s not in exclude]
        divergence_df = pd.DataFrame()
        # Generate divergence matrix
        for s in progress_bar(samples):
            divergence = self.calc_divergence(target_id=s,
                                              kde_kernel=kde_kernel,
                                              divergence_method=divergence_method,
                                              verbose=False,
                                              comparisons=samples)
            hd_ = defaultdict(list)
            for n, h in divergence:
                hd_[n].append(h)
            hd_ = pd.DataFrame(hd_)
            hd_['sample_id'] = s
            divergence_df = pd.concat([divergence_df, hd_])

        # Perform hierarchical clustering
        r = divergence_df.drop('sample_id', axis=1).values
        c = divergence_df.drop('sample_id', axis=1).T.values
        row_linkage = hierarchy.linkage(distance.pdist(r), method=clustering_method)
        col_linkage = hierarchy.linkage(distance.pdist(c), method=clustering_method)

        if divergence_method == 'jsd':
            center = 0.5
        else:
            center = 0
        g = sns.clustermap(divergence_df.set_index('sample_id'),
                           row_linkage=row_linkage, col_linkage=col_linkage,
                           method=clustering_method,
                           center=center, cmap="vlag",
                           figsize=figsize, **kwargs)
        ax = g.ax_heatmap
        ax.set_xlabel('')
        ax.set_ylabel('')
        return row_linkage, divergence_df.sample_id.values, g

    def calc_divergence(self, target_id: str, comparisons: list, kde_kernel: str = 'gaussian',
                        divergence_method: str = 'jsd', verbose: bool = False) -> np.array:
        """
        Calculate the statistical distance between the probability density function of a target sample and one or many
        comparison samples.
        :param target_id: sample ID for PDF p
        :param root_population: name of population to retrieve samples from
        :param comparisons: list of sample IDs that will form PDF q
        :param sample_n: number of cells to sample from each (optional but recommended; default = 10000)
        :param transform: method used to transform the data prior to processing (default = 'logicle')
        :param scale: scaling function to apply to data post-transformation (optional; default = None)
        :param kde_kernel: name of kernel to use for density estimation (default = 'gaussian')
        :param divergence_method: name of statistical distance metric to use; valid choices are:
            *jsd: squared Jensen-Shannon Divergence (default)
            *kl: Kullback–Leibler divergence (relative entropy); warning, asymmetrical
            *hellinger: squared Hellinger distance
        :param verbose: If True, function will return regular feedback (default = False)
        :return: List of tuples in format (SAMPLE_NAME, DIVERGENCE)
        """
        assert divergence_method in ['jsd', 'kl', 'hellinger'], 'Invalid divergence metric must be one of ' \
                                                                '[jsd, kl, hellinger]'
        if verbose:
            if divergence_method == 'kl':
                print('Warning: Kullback-Leiber Divergence chosen as statistical distance metric, KL divergence '
                      'is an asymmetrical function and as such it should not be used for generating a divergence matrix')

        # Calculate PDF for target
        if verbose:
            print('Calculating PDF for target...')
        if target_id not in self.kde_cache.keys():
            target = self.data[target_id]
            target = target.select_dtypes(include=['number']).values
            self.kde_cache[target_id] = kde_multivariant(target, bandwidth='cross_val', kernel=kde_kernel)

        # Calc PDF for other samples and calculate F-divergence
        if verbose:
            print(f'Calculate PDF for all other samples and calculate F-divergence metric: {divergence_method}...')

        # Fetch data frame for all other samples if kde not previously computed
        samples_df = [(name, s.select_dtypes(include='number').values) for name, s in self.data.items()
                      if name in comparisons and name not in self.kde_cache.keys()]
        kde_f = partial(kde_multivariant, bandwidth='cross_val', kernel=kde_kernel)
        kde_indexed_f = partial(indexed_kde, kde_f=kde_f)
        pool = Pool(cpu_count())
        q_ = pool.map(kde_indexed_f, samples_df)
        pool.close()
        pool.join()
        for name, q in q_:
            self.kde_cache[name] = q
        if divergence_method == 'jsd':
            return [(name, jsd_divergence(self.kde_cache[target_id], q)) for name, q in self.kde_cache.items()]
        if divergence_method == 'kl':
            return [(name, kl_divergence(self.kde_cache[target_id], q)) for name, q in self.kde_cache.items()]
        return [(name, hellinger_dot(self.kde_cache[target_id], q)) for name, q in self.kde_cache.items()]
