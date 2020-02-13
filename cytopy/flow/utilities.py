from cytopy.data.fcs_experiments import FCSExperiment
from cytopy.flow.gating.actions import Gating
from cytopy.flow.supervised.utilities import scaler
from IPython import get_ipython
from tqdm import tqdm_notebook, tqdm
from sklearn.neighbors import BallTree, KernelDensity
from sklearn.model_selection import GridSearchCV
from scipy.stats import entropy as kl_divergence
import pandas as pd
import numpy as np


def which_environment():
    """
    Test if module is being executed in the Jupyter environment.
    :return:
    """
    try:
        ipy_str = str(type(get_ipython()))
        if 'zmqshell' in ipy_str:
            return 'jupyter'
        if 'terminal' in ipy_str:
            return 'ipython'
    except:
        return 'terminal'


def progress_bar(x: iter, **kwargs) -> callable:
    """
    Generate a progress bar using the tqdm library. If execution environment is Jupyter, return tqdm_notebook
    otherwise used tqdm.
    :param x: some iterable to pass to tqdm function
    :param kwargs: additional keyword arguments for tqdm
    :return: tqdm or tqdm_notebook, depending on environment
    """
    if which_environment() == 'jupyter':
        return tqdm_notebook(x, **kwargs)
    return tqdm(x, **kwargs)


def faithful_downsampling(data: np.array, h: float):
    """
    An implementation of faithful downsampling as described in:  Zare H, Shooshtari P, Gupta A, Brinkman R.
    Data reduction for spectral clustering to analyze high throughput flow cytometry data. BMC Bioinformatics 2010;11:403
    :param data: numpy array to be downsampled
    :param h: radius for nearest neighbours search
    :return: Downsampled array
    """
    communities = None
    registered = np.zeros(data.shape[0])
    tree = BallTree(data)
    while not all([x == 1 for x in registered]):
        i_ = np.random.choice(np.where(registered == 0)[0])
        registered[i_] = 1
        registering_idx = tree.query_radius(data[i_].reshape(1, -1), r=h)[0]
        registering_idx = [t for t in registering_idx if t != i_]
        registered[registering_idx] = 1
        if communities is None:
            communities = data[registering_idx]
        else:
            communities = np.unique(np.concatenate((communities, data[registering_idx]), 0), axis=0)
    return communities


def hellinger_dot(p, q):
    """
    Hellinger distance between two discrete distributions.
    Original code found here: https://nbviewer.jupyter.org/gist/Teagum/460a508cda99f9874e4ff828e1896862
    :param p: discrete probability distribution, p
    :param q: discrete probability distribution, q
    :return: Hellinger Distance
    """
    z = np.sqrt(p) - np.sqrt(q)
    return np.sqrt(z @ z / 2)


def jsd_divergence(p, q):
    """
    Calculate the Jensen-Shannon Divergence between two PDFs
    :param p:
    :param q:
    :return:
    """
    m = (p + q)/2
    divergence = (kl_divergence(p, m) + kl_divergence(q, m)) / 2
    return np.sqrt(divergence)


def kde_multivariant(x: np.array, bandwidth: str or float = 'cross_val',
                     bandwidth_search: list or None = None, x_grid_n: int or None = 1000, **kwargs):
    if type(bandwidth) == str:
        assert bandwidth == 'cross_val', 'Invalid input for bandwidth, must be either float or "cross_val"'
        if bandwidth_search is None:
            bandwidth_search = [np.quantile(x, 0.05), np.quantile(x, 0.95)]
            if bandwidth_search == 0:
                bandwidth_search[0] = 0.01
        grid = GridSearchCV(KernelDensity(),
                            {'bandwidth': np.linspace(bandwidth_search[0], bandwidth_search[1], 30)},
                            cv=20)
        grid.fit(x)
        bandwidth = grid.best_estimator_.bandwidth
    kde = KernelDensity(bandwidth=bandwidth, **kwargs)
    kde.fit(x)
    if x_grid_n is not None:
        x_grid = np.array([np.linspace(np.amin(x), np.amax(x), x_grid_n) for _ in range(x.shape[1])])
        log_pdf = kde.score_samples(x_grid.T)
    else:
        log_pdf = kde.score_samples(x)
    return np.exp(log_pdf)


def load_and_transform(sample_id: str, experiment: FCSExperiment, root_population: str, transform: str or None,
                       scale: str or None = None, sample_n: int or None = None) -> pd.DataFrame or None:
    """
    Standard function for loading data from an experiment, transforming, scaling, and sampling.
    :param experiment:
    :param sample_id:
    :param root_population:
    :param transform:
    :param scale:
    :param sample_n:
    :return:
    """
    gating = Gating(experiment=experiment, sample_id=sample_id, include_controls=False)
    if transform is None:
        data = gating.get_population_df(root_population,
                                        transform=False,
                                        transform_features='all')
    else:
        data = gating.get_population_df(root_population,
                                        transform=True,
                                        transform_method=transform,
                                        transform_features='all')
    if scale is not None:
        data = scaler(data, scale_method=scale)[0]
    if data is None:
        raise KeyError(f'Error: unable to load data for population {root_population} for {sample_id}')
    if sample_n is not None:
        if data.shape[0] < sample_n:
            print(f'{sample_id} contains less rows than the specified sampling n {sample_n}, returning unsampled dataframe')
            return data
        return data.sample(sample_n)
    return data


def ordered_load_transform(sample_id: str, experiment: FCSExperiment, root_population: str, transform: str,
                           scale: str or None = None, sample_n: int or None = None):
    try:
        data = load_and_transform(sample_id, experiment, root_population, transform,
                                  scale, sample_n)
    except KeyError:
        print(f'Sample {sample_id} missing root population {root_population}')
        return sample_id, None
    return sample_id, data