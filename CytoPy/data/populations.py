from functools import reduce
from shapely.ops import unary_union
from typing import List
from _warnings import warn
import numpy as np
import pandas as pd
import mongoengine

from CytoPy.data.geometry import PopulationGeometry, ThresholdGeom, PolygonGeom


class Cluster(mongoengine.EmbeddedDocument):
    """
    Represents a single cluster generated by a clustering experiment on a single file

    Parameters
    ----------
    cluster_id: str, required
        name associated to cluster
    index: FileField
        index of cell events associated to cluster (very large array)
    n_events: int, required
        number of events in cluster
    prop_of_root: float, required
        proportion of events in cluster relative to root population
    cluster_experiment: RefField
        reference to ClusteringDefinition
    meta_cluster_id: str, optional
        associated meta-cluster
    """
    cluster_id = mongoengine.StringField(required=True)
    meta_label = mongoengine.StringField(required=False)
    n = mongoengine.IntField(required=True)
    prop_of_events = mongoengine.FloatField(required=True)
    tag = mongoengine.StringField(required=True)

    def __init__(self, *args, **kwargs):
        self._index = kwargs.pop("index", None)
        super().__init__(*args, **kwargs)

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, idx: np.array or list):
        self.n = len(idx)
        self._index = np.array(idx)


class Population(mongoengine.EmbeddedDocument):
    """
    Cached populations; stores the index of events associated to a population for quick loading.

    Parameters
    ----------
    population_name: str, required
        name of population
    index: FileField
        numpy array storing index of events that belong to population
    prop_of_parent: float, required
        proportion of events as a percentage of parent population
    prop_of_total: float, required
        proportion of events as a percentage of all events
    warnings: list, optional
        list of warnings associated to population
    parent: str, required, (default: "root")
        name of parent population
    children: list, optional
        list of child populations (list of strings)
    geom: list, required
        list of key value pairs (tuples; (key, value)) for defining geom of population e.g.
        the definition for an ellipse that 'captures' the population
    clusters: EmbeddedDocListField
        list of associated Cluster documents
    """
    population_name = mongoengine.StringField()
    n = mongoengine.IntField()
    parent = mongoengine.StringField(required=True, default='root')
    prop_of_parent = mongoengine.FloatField()
    prop_of_total = mongoengine.FloatField()
    warnings = mongoengine.ListField()
    geom = mongoengine.EmbeddedDocumentField(PopulationGeometry)
    clusters = mongoengine.EmbeddedDocumentListField(Cluster)
    definition = mongoengine.StringField()
    signature = mongoengine.DictField()

    def __init__(self, *args, **kwargs):
        # If the Population existed previously, fetched the index
        self._index = kwargs.pop("index", None)
        self._ctrl_index = kwargs.pop("ctrl_index", dict())
        super().__init__(*args, **kwargs)

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, idx: np.array):
        assert isinstance(idx, np.ndarray), "idx should be type numpy.array"
        self.n = len(idx)
        self._index = np.array(idx)

    @property
    def ctrl_index(self):
        return self._ctrl_index

    def set_ctrl_index(self, **kwargs):
        for k, v in kwargs.items():
            assert isinstance(v, np.ndarray), "ctrl_idx should be type numpy.array"
            self._ctrl_index[k] = v

    def add_cluster(self,
                    cluster: Cluster):
        _id, tag = cluster.cluster_id, cluster.tag
        err = f"Cluster already exists with id: {_id}; tag: {tag}"
        assert not any([x.cluster_id == _id and x.tag == tag for x in self.clusters]), err
        self.clusters.append(cluster)

    def delete_clusters(self, tag: str or None, drop_all: bool = False):
        """
        Delete clusters with the given clustering definition ID or drop all clusters

        Parameters
        ----------
        tag: str
        drop_all: bool (default=False)

        Returns
        -------
        None
        """
        if drop_all:
            self.clusters = []
        else:
            assert tag is not None, 'Must provide a valid tag'
            self.clusters = [x for x in self.clusters if x.tag != tag]


def _check_overlap(left: Population,
                   right: Population,
                   error: bool = True):
    """
    Given two Population objects assuming that they have Polygon geoms (raises assertion error otherwise), checks if the population geometries overlap.
    If error is True, raises assertation error if the geometries do not overlap.
    Parameters
    ----------
    left: Population
    right: Population
    error: bool (default = True)

    Returns
    -------
    bool or None
    """
    assert all([isinstance(x.geom, PolygonGeom) for x in [left, right]]), "Only Polygon geometries can be checked for overlap"
    overlap = left.geom.shape.intersects(right.geom.shape)
    if error:
        assert overlap, "Invalid: non-overlapping populations"
    return overlap


def _check_transforms_dimensions(left: Population,
                                 right: Population):
    """
    Given two Populations, checks if transformation methods and axis match. Raises assertion error if not.

    Parameters
    ----------
    left: Population
    right: Population

    Returns
    -------
    None
    """
    assert left.geom.transform_x == right.geom.transform_x, "X dimension transform differs between left and right populations"
    assert left.geom.transform_y == right.geom.transform_y, "Y dimension transform differs between left and right populations"
    assert left.geom.x == right.geom.x, "X dimension differs between left and right populations"
    assert left.geom.y == right.geom.y, "Y dimension differs between left and right populations"


def _merge_index(left: Population,
                 right: Population):
    return np.unique(np.concatenate([left.index, right.index], axis=0), axis=0)


def _merge_signatures(left: Population,
                      right: Population):
    return pd.DataFrame([left.signature, right.signature]).mean().to_dict()


def _merge_thresholds(left: Population,
                      right: Population,
                      new_population_name: str):
    assert left.geom.x_threshold == right.geom.x_threshold, "Threshold merge assumes that the populations are derived from the same gate; X threshold should match between " \
                                                            "populations"
    assert left.geom.y_threshold == right.geom.y_threshold, "Threshold merge assumes that the populations are derived from the same gate; Y threshold should match between " \
                                                            "populations"
    if left.clusters or right.clusters:
        warn("Associated clusters are now void. Repeat clustering on new population")
        left.clusters, right_clusters = [], []
    if len(left.ctrl_index) > 0 or len(right.ctrl_index) > 0:
        warn("Associated control indexes are now void. Repeat control gating on new population")
    new_geom = ThresholdGeom(x=left.geom.x,
                             y=left.geom.y,
                             transform_x=left.geom.transform_x,
                             transform_y=left.geom.transform_y,
                             x_threshold=left.geom.x_threshold,
                             y_threshold=left.geom.y_threshold)

    new_population = Population(population_name=new_population_name,
                                n=len(left.index) + len(right.index),
                                parent=left.parent,
                                warnings=left.warnings + right.warnings + ["MERGED POPULATION"],
                                index=_merge_index(left, right),
                                geom=new_geom,
                                definition=",".join([left.definition, right.definition]),
                                signature=_merge_signatures(left, right))
    return new_population


def _merge_polygons(left: Population,
                    right: Population,
                    new_population_name: str):
    _check_overlap(left, right)
    new_shape = unary_union([p.geom.shape for p in [left, right]])
    x, y = new_shape.exterior.coords.xy
    new_geom = PolygonGeom(x=left.geom.x,
                           y=left.geom.y,
                           transform_x=left.geom.transform_x,
                           transform_y=left.geom.transform_y,
                           x_values=x,
                           y_values=y)
    new_population = Population(population_name=new_population_name,
                                n=len(left.index) + len(right.index),
                                parent=left.parent,
                                warnings=left.warnings + right.warnings + ["MERGED POPULATION"],
                                index=_merge_index(left, right),
                                geom=new_geom,
                                signature=_merge_signatures(left, right))
    return new_population


def merge_populations(left: Population,
                      right: Population,
                      new_population_name: str or None = None):
    _check_transforms_dimensions(left, right)
    new_population_name = new_population_name or f"merge_{left.population_name}_{right.population_name}"
    assert left.parent == right.parent, "Parent populations do not match"
    assert isinstance(left.geom, type(right.geom)), f"Geometries must be of the same type; left={type(left.geom)}, right={type(right.geom)}"
    if isinstance(left.geom, ThresholdGeom):
        return _merge_thresholds(left, right, new_population_name)
    return _merge_polygons(left, right, new_population_name)


def merge_multiple_populations(populations: List[Population],
                               new_population_name: str or None = None):
    if new_population_name is None:
        assert len(set([p.population_name for p in populations])) == 1, \
            "If a new population name is not given the populations are expected to have the same population name"
    new_population_name = new_population_name or populations[0].population_name
    merged_pop = reduce(lambda p1, p2: merge_populations(p1, p2), populations)
    merged_pop.population_name = new_population_name
    return merged_pop

