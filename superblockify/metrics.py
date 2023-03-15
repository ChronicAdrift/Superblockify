"""Metric object for the superblockify package."""
import logging
import pickle
from configparser import ConfigParser
from datetime import timedelta
from itertools import product, combinations, chain
from multiprocessing import cpu_count, Pool
from os import path
from time import time

import numpy as np
from matplotlib import pyplot as plt
from networkx import to_scipy_sparse_array
from osmnx.projection import is_projected
from scipy.sparse.csgraph import dijkstra
from tqdm import tqdm

from superblockify.plot import plot_distance_distributions

logger = logging.getLogger("superblockify")

config = ConfigParser()
config.read("config.ini")
RESULTS_DIR = config["general"]["results_dir"]

_AVG_EARTH_RADIUS_M = 6.3781e6  # in meters, arXiv:1510.07674 [astro-ph.SR]


class Metric:
    """Metric object to be used with partitioners.

    A metric object is used to calculate the quality of a partitioning.
    It holds the information on several network metrics, which can be read,
    and can be used to calculate them when passing a Partitioner object.

    There are different network measures
    - d_E(i, j): Euclidean
    - d_S(i, j): Shortest path on full graph
    - d_N(i, j): Shortest path with ban through LTNs

    Attributes
    ----------
    coverage : float
        The coverage of the partitioning takes of the whole graph
    num_components : int
        The number of components in the graph
    avg_path_length : dict
        The average path length of the graph for each network measure
        {"E": float, "S": float, "N": float}
    directness : dict
        The directness of the graph for the network measure ratios
        {"ES": float, "EN": float, "SN": float}
    global_efficiency : dict
        The global efficiency of the graph for each network measure
        {"SE": float, "NE": float, "NS": float}
    local_efficiency : dict
        The local efficiency of the graph for each network measure
        {"SE": float, "NE": float, "NS": float}

    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self):
        """Construct a metric object."""

        self.coverage = None
        self.num_components = None
        self.avg_path_length = {"E": None, "S": None, "N": None}
        self.directness = {"ES": None, "EN": None, "SN": None}
        self.global_efficiency = {"SE": None, "NE": None, "NS": None}
        self.local_efficiency = {"SE": None, "NE": None, "NS": None}

        self.distance_matrix = None
        self.weight = None

    def calculate_all(
        self,
        partitioner,
        weight="length",
        num_workers=None,
        chunk_size=1,
        make_plots=False,
    ):
        """Calculate all metrics for the partitioning.

        `self.distance_matrix` is used to save the distances for the metrics and should
        is set to None after calculating the metrics.

        Parameters
        ----------
        partitioner : BasePartitioner
            The partitioner object to calculate the metrics for
        weight : str, optional
            The edge attribute to use as weight, by default "length", if None count hops
        num_workers : int, optional
            The number of workers to use for multiprocessing. If None, use
            min(32, os.cpu_count() + 4), by default None
        chunk_size : int, optional
            The chunk size to use for multiprocessing, by default 1
        make_plots : bool, optional
            Whether to make plots of the distributions of the distances for each
            network measure, by default False

        """
        # pylint: disable=unused-argument

        # Set weight attribute
        self.weight = weight

        node_list = partitioner.get_sorted_node_list()

        # Euclidean distances (E)
        dist_euclidean = self.calculate_euclidean_distance_matrix_projected(
            partitioner.graph,
            node_order=node_list,
            plot_distributions=make_plots,
        )

        # On the full graph (S)
        dist_full_graph = self.calculate_distance_matrix(
            partitioner.graph,
            weight="length",
            node_order=node_list,
            plot_distributions=make_plots,
        )

        # On the partitioning graph (N)
        dist_partitioning_graph = self.calculate_partitioning_distance_matrix(
            partitioner,
            weight="length",
            node_order=node_list,
            num_workers=num_workers,
            chunk_size=chunk_size,
            plot_distributions=make_plots,
        )

        self.distance_matrix = {
            "E": dist_euclidean,
            "S": dist_full_graph,
            "N": dist_partitioning_graph,
        }

        self.calculate_all_measure_sums()

        # self.coverage = self.calculate_coverage(partitioner)
        # logger.debug("Coverage: %s", self.coverage)

    def calculate_all_measure_sums(self):
        """Based on the distance matrix, calculate the network measures.

        Calculate the directness, global and local efficiency for each network measure
        and write them to the corresponding attributes.

        """

        # Directness
        for key in self.directness:
            self.directness[key] = self.calculate_directness(key[0], key[1])
            logger.debug("Directness %s: %s", key, self.directness[key])

        # Global efficiency
        for key in self.global_efficiency:
            self.global_efficiency[key] = self.calculate_global_efficiency(
                key[0], key[1]
            )
            logger.debug("Global efficiency %s: %s", key, self.global_efficiency[key])

        # # Local efficiency
        # for key in self.local_efficiency:
        #     self.local_efficiency[key] = self.calculate_local_efficiency(key[0], key[1])
        #     logger.debug("Local efficiency %s: %s", key, self.local_efficiency[key])

    def calculate_directness(self, measure1, measure2):
        r"""Calculate the directness for the given network measures.

        The directness in the mean of the ratios between the distances of the two
        network measures.

        If any of the distances is 0 or infinite, it is ignored in the calculation.

        Parameters
        ----------
        measure1 : str
            The first network measure
        measure2 : str
            The second network measure

        Returns
        -------
        float
            The directness of the network measures

        Notes
        -----
        .. math:: D_{E/S}=\left\langle\frac{d_E(i, j)}{d_S(i, j)}\right\rangle_{i\neq j}
        """

        dist1, dist2 = self._network_measures_filtered_flattened(measure1, measure2)

        # Calculate the directness as the mean of the ratios
        return np.mean(dist1 / dist2)

    def calculate_global_efficiency(self, measure1, measure2):
        r"""Calculate the global efficiency for the given network measures.

        The global efficiency is the ratio between the sums of the inverses of the
        distances of the two network measures.

        If any of the distances is 0 or infinite, it is ignored in the calculation.

        Parameters
        ----------
        measure1 : str
            The first network measure
        measure2 : str
            The second network measure

        Returns
        -------
        float
            The global efficiency of the network measures

        Notes
        -----
        .. math::

             E_{\text{glob},S/E}=\frac{\sum_{i \neq j}\frac{1}{d_S(i, j)}}
             {\sum_{i \neq j} \frac{1}{d_E(i, j)}}
        """

        dist1, dist2 = self._network_measures_filtered_flattened(measure1, measure2)

        # Calculate the global efficiency as the ratio between the sums of the inverses
        return np.sum(1 / dist1) / np.sum(1 / dist2)

    def _network_measures_filtered_flattened(self, measure1, measure2):
        """Return the two network measures filtered and flattened.

        The diagonal is set to 0 and the matrix is flattened. We use flattening as it
        preserves the order of the distances and makes the calculation faster.

        Parameters
        ----------
        measure1 : str
            The first network measure
        measure2 : str
            The second network measure

        Returns
        -------
        1d ndarray
            The first network measure
        1d ndarray
            The second network measure
        """

        # Get the distance matrix for the two network measures
        dist1 = self.distance_matrix[measure1]
        dist2 = self.distance_matrix[measure2]
        # Set the diagonal to 0 so that it is not included in the calculation
        np.fill_diagonal(dist1, 0)
        np.fill_diagonal(dist2, 0)
        # Flatten the distance matrices
        dist1 = dist1.flatten()
        dist2 = dist2.flatten()

        # Drop the pairs of distances where at least one is 0 or infinite
        mask = np.logical_and(dist1 != 0, dist2 != 0)
        mask = np.logical_and(mask, np.isfinite(dist1), np.isfinite(dist2))
        dist1 = dist1[mask]
        dist2 = dist2[mask]

        return dist1, dist2

    def calculate_local_efficiency(self, measure1, measure2):
        r"""Calculate the local efficiency for the given network measures.

        The local efficiency is like the average of the global efficiency for each
        node. It is the mean of the ratios between the sums of the inverses of the
        distances of the two network measures for each node.

        Parameters
        ----------
        measure1 : str
            The first network measure
        measure2 : str
            The second network measure

        Returns
        -------
        float
            The local efficiency of the network measures

        Notes
        -----
        .. math::

            E_{\mathrm{loc},S/E}=\frac{1}{N} \sum_{i=1}^{N} E_{\mathrm{glob},S/E}(i)

        """

        # Get the distance matrix for the two network measures
        dist1 = self.distance_matrix[measure1]
        dist2 = self.distance_matrix[measure2]

        # Mask the diagonal, 0 and infinite values
        mask = np.logical_and(dist1 != 0, dist2 != 0)
        mask = np.logical_and(mask, np.isfinite(dist1), np.isfinite(dist2))
        np.fill_diagonal(mask, False)
        # Filter distances using the mask
        # dist1 = np.ma.masked_array(dist1, mask=mask)
        # dist2 = np.ma.masked_array(dist2, mask=mask)
        # Another way
        # dist1 = dist1[mask]
        # dist2 = dist2[mask]
        # Another way
        dist1 = np.where(mask, dist1, np.inf)
        dist2 = np.where(mask, dist2, np.inf)
        # Is this correct? We get efficiencies below 1%.

        # Calculate the global efficiency for each row
        efficiency = np.sum(1 / dist1, axis=1) / np.sum(1 / dist2, axis=1)

        # Calculate the local efficiency as the mean of the global efficiencies
        print(np.sum(efficiency))
        print(np.sum(~mask))
        # The sum of the efficiency sum is zero.
        # This is because the mask is not applied correctly.
        # It should be applied as follows:
        # np.sum(efficiency) / np.sum(~mask)
        return np.sum(efficiency) / np.sum(~mask)

    def calculate_coverage(self, partitioner):
        """Calculate the coverage of the partitioner.

        Calculates the coverage of the partitions weighted by the edge attribute
        self.weight. The coverage is the sum of the weights of the edges between


        Parameters
        ----------
        partitioner : Partitioner
            The partitioner to calculate the coverage for

        Other Parameters
        ----------------
        weight : str
            The edge attribute to use as weight. It is passed saved in self.weight.
        """

        subgraph_edges = [
            part["subgraph"].edges(data=True)
            for part in partitioner.get_partition_nodes()
        ]

        return sum(d[self.weight] for u, v, d in subgraph_edges) / sum(
            d[self.weight] for u, v, d in partitioner.graph.edges(data=True)
        )

    def __str__(self):
        """Return a string representation of the metric object.

        Only returns the attributes that are not None or for a dict the
        attributes that are not None for each key. If all attributes in a dict are None,
        it is not returned.
        If no attributes are not None, an empty string is returned.
        """
        string = ""
        for key, value in self.__dict__.items():
            if value is not None:
                if isinstance(value, dict):
                    if all(v is None for v in value.values()):
                        continue
                    string += f"{key}: "
                    for key2, value2 in value.items():
                        if value2 is not None:
                            string += f"{key2}: {value2}, "
                    string = string[:-2] + "; "
                else:
                    string += f"{key}: {value}; "
        return string

    def __repr__(self):
        """Return a string representation of the metric object.

        Additional to the __str__ method, it also returns the class name.
        """
        return f"{self.__class__.__name__}({self.__str__()})"

    def __eq__(self, other):
        """Return True if the two objects are equal.

        Tests the equality of the attributes of the objects.
        Used in input-output tests.
        """
        return self.__dict__ == other.__dict__

    def calculate_distance_matrix(
        self,
        graph,
        weight=None,
        node_order=None,
        plot_distributions=False,
        log_debug=True,
    ):
        """Calculate the distance matrix for the partitioning.

        Use cythonized scipy.sparse.csgraph functions to calculate the distance matrix.

        Generally Dijkstra's algorithm with a Fibonacci heap is used. It's approximate
        computational cost is ``O[N(N*k + N*log(N))]`` where ``N`` is the number of
        nodes and ``k`` is the average number of edges per node. We use this,
        because our graphs are usually sparse. For dense graphs, the Floyd-Warshall
        algorithm can be implemented with ``O[N^3]`` computational cost. [1]_

        Runtime comparison:
        - Scheveningen, NL (N = 1002, E = 2329):
            - Dijkstra: 172ms
            - Floyd-Warshall: 193ms
        - Liechtenstein, LI (N = 1797, E = 4197):
            - Dijkstra: 498ms
            - Floyd-Warshall: 917ms
        - Copenhagen, DK (N = 7806, E = 19565):
            - Dijkstra: 14.65s
            - Floyd-Warshall: 182.03s
        - Barcelona, ES (N = 8812, E = 16441):
            - Dijkstra: 18.21s
            - Floyd-Warshall: 134.69s
        (simple, one-time execution)

        The input graph will be converted to a scipy sparse matrix in CSR format.
        Compressed Sparse Row format is a sparse matrix format that is efficient for
        arithmetic operations. [2]_

        Parameters
        ----------
        graph : networkx.Graph
            The graph to calculate the distance matrix for
        weight : str, optional
            The edge attribute to use as weight. If None, all edge weights are 1.
        node_order : list, optional
            The order of the nodes in the distance matrix. If None, the ordering is
            produced by graph.nodes().
        plot_distributions : bool, optional
            If True, a histogram of the distribution of the shortest path lengths is
            plotted.
        log_debug : bool, optional
            If True, log runtime and graph information at debug level.

        Raises
        ------
        ValueError
            If the graph has negative edge weights.

        Returns
        -------
        dist_matrix : ndarray
            The distance matrix for the partitioning. dist_matrix[i, j] is the shortest
            path length from node i to node j.

        References
        ----------
        .. [1] SciPy 1.10.0 Reference Guide, scipy.sparse.csgraph.shortest_path
           https://docs.scipy.org/doc/scipy-1.10.0/reference/generated/scipy.sparse.csgraph.shortest_path.html
           (accessed February 21, 2023)
        .. [2] SciPy 1.10.0 Reference Guide, scipy.sparse.csr_matrix
           https://docs.scipy.org/doc/scipy-1.10.0/reference/generated/scipy.sparse.csr_matrix.html
           (accessed February 21, 2023)

        """

        if weight is not None and any(
            w < 0 for (u, v, w) in graph.edges.data(weight, default=0)
        ):
            # For this case Johnson's algorithm could be used, but none of our graphs
            # should have negative edge weights.
            raise ValueError("Graph has negative edge weights.")

        # First get N x N array of distances representing the input graph.
        graph_matrix = to_scipy_sparse_array(
            graph, weight=weight, format="csr", nodelist=node_order
        )
        start_time = time()
        dist_full_graph = dijkstra(
            graph_matrix, directed=True, return_predecessors=False, unweighted=False
        )

        # Convert to half-precision to save memory
        dist_full_graph = dist_full_graph.astype(np.half)

        if log_debug:
            logger.debug(
                "All-pairs shortest path lengths for graph with %s nodes and %s edges "
                "calculated in %s.",
                graph.number_of_nodes(),
                graph.number_of_edges(),
                timedelta(seconds=time() - start_time),
            )
        if plot_distributions:
            if node_order is None:
                node_order = graph.nodes()
            # Where `dist_full_graph` is inf, replace with 0
            plot_distance_distributions(
                dist_full_graph[dist_full_graph != np.inf],
                dist_title="Distribution of shortest path lengths on full graph",
                coords=(
                    [graph.nodes[node]["x"] for node in node_order],
                    [graph.nodes[node]["y"] for node in node_order],
                ),
                coord_title="Coordinates of nodes",
                labels=("x", "y"),
                distance_unit="khops"
                if weight is None
                else "km"
                if weight == "length"
                else f"k{weight}",
            )

        return dist_full_graph

    def calculate_euclidean_distance_matrix_projected(
        self, graph, node_order=None, plot_distributions=False
    ):
        """Calculate the euclidean distances between all nodes in the graph.

        Uses the x and y coordinates of the nodes of a projected graph. The coordinates
        are in meters.

        Parameters
        ----------
        graph : networkx.Graph
            The graph to calculate the distance matrix for. The graph should be
            projected.
        node_order : list, optional
            The order of the nodes in the distance matrix. If None, the ordering is
            produced by graph.nodes().
        plot_distributions : bool, optional
            If True, plot the distributions of the euclidean distances and coordinates.
            Sanity check for the coordinate values.

        Returns
        -------
        dist_matrix : ndarray
            The distance matrix for the partitioning. dist_matrix[i, j] is the euclidean
            distance between node i and node j.

        Raises
        ------
        ValueError
            If the graph is not projected.

        """

        # Find CRS from graph's metadata
        if "crs" not in graph.graph or not is_projected(graph.graph["crs"]):
            raise ValueError("Graph is not projected.")

        # Get the node order
        if node_order is None:
            node_order = list(graph.nodes())

        # Get the coordinates of the nodes
        x_coord = np.array([graph.nodes[node]["x"] for node in node_order])
        y_coord = np.array([graph.nodes[node]["y"] for node in node_order])

        # Check that all values are float or int and not inf or nan
        if not np.issubdtype(x_coord.dtype, np.number) or not np.issubdtype(
            y_coord.dtype, np.number
        ):
            raise ValueError("Graph has non-numeric coordinates.")
        if np.any(np.isinf(x_coord)) or np.any(np.isinf(y_coord)):
            raise ValueError("Graph has infinite coordinates.")

        # Calculate the euclidean distances between all nodes
        dist_matrix = np.sqrt(
            np.square(x_coord[:, np.newaxis] - x_coord[np.newaxis, :])
            + np.square(y_coord[:, np.newaxis] - y_coord[np.newaxis, :])
        )
        # Convert to half-precision to save memory
        dist_matrix = dist_matrix.astype(np.half)

        if plot_distributions:
            plot_distance_distributions(
                dist_matrix,
                dist_title="Distribution of euclidean distances",
                coords=(x_coord, y_coord),
                coord_title="Scatter plot of projected coordinates",
                labels=("x", "y"),
            )

        return dist_matrix

    def calculate_euclidean_distance_matrix_haversine(
        self, graph, node_order=None, plot_distributions=False
    ):
        """Calculate the euclidean distances between all nodes in the graph.

        Uses the **Haversine formula** to calculate the distances between all nodes in
        the graph. The coordinates are in degrees.

        Parameters
        ----------
        graph : networkx.Graph
            The graph to calculate the distance matrix for
        node_order : list, optional
            The order of the nodes in the distance matrix. If None, the ordering is
            produced by graph.nodes().
        plot_distributions : bool, optional
            If True, plot the distributions of the euclidean distances and coordinates.
            Sanity check for the coordinate values.

        Returns
        -------
        dist_matrix : ndarray
            The distance matrix for the partitioning. dist_matrix[i, j] is the euclidean
            distance between node i and node j.

        Raises
        ------
        ValueError
            If coordinates are not numeric or not in the range [-90, 90] for latitude
            and [-180, 180] for longitude.

        """

        if node_order is None:
            node_order = list(graph.nodes())

        start_time = time()

        # Calculate the euclidean distances between all nodes
        # Do vectorized calculation for all nodes
        lat = np.array([graph.nodes[node]["lat"] for node in node_order])
        lon = np.array([graph.nodes[node]["lon"] for node in node_order])

        # Check that all values are float or int and proper lat/lon values
        if not np.issubdtype(lat.dtype, np.number) or not np.issubdtype(
            lon.dtype, np.number
        ):
            raise ValueError("Latitude and longitude values must be numeric.")
        if np.any(lat > 90) or np.any(lat < -90):
            raise ValueError("Latitude values are not in the range [-90, 90].")
        if np.any(lon > 180) or np.any(lon < -180):
            raise ValueError("Longitude values are not in the range [-180, 180].")

        node1_lat = np.expand_dims(lat, axis=0)
        node1_lon = np.expand_dims(lon, axis=0)
        node2_lat = np.expand_dims(lat, axis=1)
        node2_lon = np.expand_dims(lon, axis=1)

        # Calculate haversine distance,
        # see https://en.wikipedia.org/wiki/Haversine_formula
        # and https://github.com/mapado/haversine/blob/master/haversine/haversine.py
        lat = node2_lat - node1_lat
        lon = node2_lon - node1_lon
        hav = (
            np.sin(lat / 2) ** 2
            + np.cos(node1_lat) * np.cos(node2_lat) * np.sin(lon / 2) ** 2
        )
        dist_matrix = 2 * _AVG_EARTH_RADIUS_M * np.arcsin(np.sqrt(hav))
        logger.debug(
            "Euclidean distances for graph with %s nodes and %s edges "
            "calculated in %s. "
            "Min/max lat/lon values: %s, %s, %s, %s; Difference: %s, %s",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            timedelta(seconds=time() - start_time),
            np.min(node1_lat),
            np.max(node1_lat),
            np.min(node1_lon),
            np.max(node1_lon),
            np.max(node1_lat) - np.min(node1_lat),
            np.max(node1_lon) - np.min(node1_lon),
        )

        if plot_distributions:
            # Plot distribution of distances and scatter plot of lat/lon
            plot_distance_distributions(
                dist_matrix,
                dist_title="Distribution of euclidean distances",
                coords=(node1_lon, node1_lat),
                coord_title="Scatter plot of lat/lon",
                labels=("Longitude", "Latitude"),
            )

        return dist_matrix

    def calculate_partitioning_distance_matrix(
        self,
        partitioner,
        weight=None,
        node_order=None,
        num_workers=None,
        chunk_size=1,
        plot_distributions=False,
        check_overlap=True,
    ):  # pylint: disable=too-many-locals
        """Calculate the distance matrix for the partitioning.

        This is the pairwise distance between all pairs of nodes, where the shortest
        paths are only allowed to traverse edges in the start and goal partitions and
        unpartitioned edges.
        For this, for each combination of start and goal partitions, the shortest
        paths are calculated using `calculate_distance_matrix()`, as well as for the
        unpartitioned edges.
        Finally, a big distance matrix is constructed, where the distances for the
        edges in the start and goal partitions are taken from the distance matrix for
        the corresponding partition, and the distances for the unpartitioned edges are
        taken from the distance matrix for the unpartitioned edges.

        Parameters
        ----------
        partitioner : BasePartitioner
            The partitioner to calculate the distance matrix for
        weight : str, optional
            The edge attribute to use as weight. If None, all edges have weight 1.
        node_order : list, optional
            The order of the nodes in the distance matrix. If None, the ordering is
            produced by graph.nodes().
        num_workers : int, optional
            The maximal number of workers used to process distance matrices. If None,
            the number of workers is set to min(32, cpu_count() // 2).
            Choose this number carefully, as it can lead to memory errors if too high,
            if the graph has partitions. In this case another partitioner approach
            might yield better results.
        chunk_size : int, optional
            The chunk-size to use for the multiprocessing pool. This is the number of
            partitions for which the distance matrix is calculated in one go (thread).
            Keep this low if the graph is big or has many partitions. We suggest to
            keep this at 1.
        plot_distributions : bool, optional
            If True, plot the distributions of the euclidean distances and coordinates.
        check_overlap : bool, optional
            If True, check that the partitions do not overlap node-wise.

        Raises
        ------
        ValueError
            If the partitions overlap node-wise. For nodes considered to be in the
            partition, see `BasePartitioner.get_partition_nodes()`.

        Returns
        -------
        dist_matrix : ndarray
            The distance matrix for the partitioning. dist_matrix[i, j] is the distance
            between node i and node j for the given rules of the partitioning.

        """
        if node_order is None:
            node_order = list(partitioner.get_sorted_node_list())

        if num_workers is None:
            num_workers = min(32, cpu_count() // 2)
            logger.debug("No number of workers specified, using %s.", num_workers)

        start_time = time()

        partitions = partitioner.get_partition_nodes()

        # Check that none of the partitions overlap by checking that the intersection
        # of the nodes in each partition is empty.
        if check_overlap:
            pairwise_overlap = self._has_pairwise_overlap(
                [list(part["nodes"]) for part in partitions]
            )
            # Check if any element in the pairwise overlap matrix is True, except the
            # diagonal
            if np.any(pairwise_overlap[np.triu_indices_from(pairwise_overlap, k=1)]):
                raise ValueError(
                    "The partitions overlap node-wise. This is not allowed."
                )

        # Get unpartitioned nodes
        # as partitioner.graph.nodes() - all nodes in partitions
        unpartitioned_nodes = set(partitioner.graph.nodes()) - set(
            node for part in partitions for node in part["nodes"]
        )

        # Preparing the combinations for processing. Generator of tuples of the form:
        # (name, sparse_matrix, node_id_order, from_indices, to_indices)
        # name: name of the processing combination
        # sparse_matrix: the sparse matrix to calculate the distances for
        # node_id_order: the order of the nodes in the sparse matrix which should be
        #                fully calculated
        # from_indices: the indices of the dijkstra result to save the distances from
        # to_indices: the indices of the distance matrix to save the distances to
        #             (the indices are the indices in the node_id_order)
        # This is done so not every combination also calculates the distances for the
        # unpartitioned edges, but only the ones that need it.

        logger.debug("Preparing combinations for processing.")

        # Start <> Goal
        combs = (
            (
                f"{start['name']}<>{goal['name']}",
                to_scipy_sparse_array(
                    partitioner.graph,
                    weight=weight,
                    format="csr",
                    nodelist=list(start["nodes"])
                    + list(goal["nodes"])
                    + list(unpartitioned_nodes),
                ),
                np.arange(len(start["nodes"]) + len(goal["nodes"])),
                [
                    np.ix_(
                        range(len(start["nodes"])),
                        range(
                            len(start["nodes"]),
                            len(start["nodes"]) + len(goal["nodes"]),
                        ),
                    ),
                    np.ix_(
                        range(
                            len(start["nodes"]),
                            len(start["nodes"]) + len(goal["nodes"]),
                        ),
                        range(len(start["nodes"])),
                    ),
                ],
                [
                    np.ix_(
                        [node_order.index(n) for n in start["nodes"]],
                        [node_order.index(n) for n in goal["nodes"]],
                    ),
                    np.ix_(
                        [node_order.index(n) for n in goal["nodes"]],
                        [node_order.index(n) for n in start["nodes"]],
                    ),
                ],
            )
            for (start, goal) in combinations(partitions, 2)
        )

        # Start == Goal + Sparsified (unpartitioned edges)
        combs = chain(
            combs,
            (
                (
                    f"{part['name']}+Sparsified",
                    to_scipy_sparse_array(
                        partitioner.graph,
                        weight=weight,
                        format="csr",
                        nodelist=list(part["nodes"]) + list(unpartitioned_nodes),
                    ),
                    np.arange(len(part["nodes"]) + len(unpartitioned_nodes)),
                    # Index with all items, don't need to split
                    [
                        np.ix_(
                            range(len(part["nodes"]) + len(unpartitioned_nodes)),
                            range(len(part["nodes"]) + len(unpartitioned_nodes)),
                        )
                    ],
                    [
                        np.ix_(
                            [
                                node_order.index(n)
                                for n in list(part["nodes"]) + list(unpartitioned_nodes)
                            ],
                            [
                                node_order.index(n)
                                for n in list(part["nodes"]) + list(unpartitioned_nodes)
                            ],
                        ),
                    ],
                )
                for part in partitions
            ),
        )

        # Only Sparsified (unpartitioned edges)
        combs = chain(
            combs,
            (
                (
                    "unp",
                    to_scipy_sparse_array(
                        partitioner.graph,
                        weight=weight,
                        format="csr",
                        nodelist=list(unpartitioned_nodes),
                    ),
                    np.arange(len(unpartitioned_nodes)),
                    # Index with all items, don't need to split
                    [
                        np.ix_(
                            range(len(unpartitioned_nodes)),
                            range(len(unpartitioned_nodes)),
                        )
                    ],
                    [
                        np.ix_(
                            [node_order.index(n) for n in list(unpartitioned_nodes)],
                            [node_order.index(n) for n in list(unpartitioned_nodes)],
                        ),
                    ],
                ),
            ),
        )

        # Calculate the combinations in parallel
        # We expect comb to be a generator of length binom(n, 2) + n + 1 = (n^2 + n) / 2
        # + 1
        logger.debug(
            "Calculating distance matrices for %s partitions, %d combinations, "
            "with %d workers and chunk-size %d.",
            len(partitions),
            (len(partitions) / 2 + 1 / 2) * len(partitions) + 1,
            num_workers,
            chunk_size,
        )
        # Parallelized calculation with `p.imap_unordered`
        with Pool(processes=num_workers) as pool:
            results = list(
                tqdm(
                    pool.imap_unordered(
                        self.dijkstra_param,
                        combs,
                        chunksize=chunk_size,
                    ),
                    desc="Calculating distance matrices",
                    total=(len(partitions) / 2 + 1 / 2) * len(partitions) + 1,
                    unit_scale=1,
                )
            )

        # Construct the distance matrix for the partitioning, half-precision float
        dist_matrix = np.full((len(node_order), len(node_order)), np.inf, dtype=np.half)
        for part_combo_dist_matrix, from_indices, to_indices in results:
            # Fill the distance matrix with the distances for the nodes in this pair
            for from_index, to_index in zip(from_indices, to_indices):
                dist_matrix[to_index] = part_combo_dist_matrix[from_index]

        logger.debug(
            "Calculated distance matrices for all combinations of partitions in %s "
            "seconds.",
            time() - start_time,
        )

        if plot_distributions:
            # Where `dist_full_graph` is inf, replace with 0
            plot_distance_distributions(
                dist_matrix[dist_matrix != np.inf],
                dist_title="Distribution of shortest path distances for the "
                "partitioning",
                coords=(
                    [partitioner.graph.nodes[node]["x"] for node in node_order],
                    [partitioner.graph.nodes[node]["y"] for node in node_order],
                ),
                coord_title="Coordinates of nodes",
                labels=("x", "y"),
                distance_unit="hops"
                if weight is None
                else "km"
                if weight == "length"
                else weight,
            )

        return dist_matrix

    @staticmethod
    def dijkstra_param(comb):
        """Wrapper for the dijkstra function.

        Fixes keyword arguments for the dijkstra function.
        """
        _, sparse_matrix, node_id_order, from_indices, to_indices = comb

        return (
            dijkstra(
                sparse_matrix,
                directed=True,
                indices=node_id_order,
                return_predecessors=False,
                unweighted=False,
            ),
            from_indices,
            to_indices,
        )

    def _calculate_pair_distance_matrix(self, graph, pair_node_order, weight):
        """Helper function to parallelize `calculate_partitioning_distance_matrix`.

        Parameters
        ----------
        graph : networkx.Graph
            The graph to calculate the distance matrix for, filtered to only include
            the nodes in `pair` and `unpartitioned_nodes`.
        pair_node_order : list
            The node order to use for the distance matrix.
        weight : str or None
            The edge attribute to use as the weight for the distance matrix. If None,
            the distance matrix is calculated using the number of hops between nodes.

        Returns
        -------
        tuple : (ndarray, list)
            A tuple of the distance matrix for the pair of partitions and the node
            order used to calculate the distance matrix.

        """
        # Make a working graph with only the edges in the partitions and
        # unpartitioned edges and calculate the distance matrix
        part_combo_dist_matrix = self.calculate_distance_matrix(
            graph,
            weight=weight,
            node_order=pair_node_order,
            log_debug=False,
        )

        return part_combo_dist_matrix

    def plot_distance_matrices(self, name=None):
        """Show the distance matrices for the network measures.

        Plots all available distance matrices in a single figure.

        Parameters
        ----------
        name : str
            The name to put into the title of the plot.

        Returns
        -------
        fig, axes : matplotlib.figure.Figure, matplotlib.axes.Axes
            The figure and axes of the plot.

        Raises
        ------
        ValueError
            If no distance matrices are available.
        """

        if self.distance_matrix is None:
            raise ValueError("No distance matrices available.")

        # Make figure with the fitting amount of subplots
        fig, axes = plt.subplots(
            1, len(self.distance_matrix), figsize=(len(self.distance_matrix) * 5, 5)
        )
        # Find maximal, non-inf value for the colorbar
        max_val = max(
            np.max(value[value != np.inf]) for value in self.distance_matrix.values()
        )
        dist_im = None
        # Subplots with shared colorbar, title, and y-axis label
        for axe, (key, value) in zip(axes, self.distance_matrix.items()):
            dist_im = axe.imshow(value, vmin=0, vmax=max_val)
            axe.set_title(f"$d_{key}(i, j)$")
            axe.set_xlabel("Node $j$")
            axe.set_aspect("equal")
        # Share y-axis
        axes[0].set_ylabel("Node $i$")
        for axe in axes[1:]:
            axe.get_shared_y_axes().join(axes[0], axe)
        # Plot colorbar on the right side of the figure
        fig.colorbar(dist_im, ax=axes, fraction=0.046, pad=0.04)
        # Label colorbar
        unit = (
            "khops"
            if self.weight is None
            else "km"
            if self.weight == "length"
            else f"k{self.weight}"
        )
        dist_im.set_label(f"Distance [{unit}]")
        # Title above all subplots
        fig.suptitle(
            f"Distance matrices for the network measures "
            f"{'(' + name + ')' if name else ''}"
        )

        return fig, axes

    def plot_distance_matrices_pairwise_relative_difference(self, name=None):
        """Show the pairwise relative difference between the distance matrices.

        Plots the pairwise relative difference between the distance matrices in a
        single figure. Only plots the lower triangle of the distance matrices.
        On the diagonal the distance matrices are plotted as in
        `plot_distance_matrices`.

        Parameters
        ----------
        name : str
            The name to put into the title of the plot.

        Returns
        -------
        fig, axes : matplotlib.figure.Figure, matplotlib.axes.Axes
            The figure and axes of the plot.

        Raises
        ------
        ValueError
            If no distance matrices are available.
        """  # pylint: disable=too-many-locals

        if self.distance_matrix is None:
            raise ValueError("No distance matrices available.")

        # Make figure with the fitting amount of subplots
        # We need len(self.distance_matrix)^2 subplots, but we only plot the lower
        # triangle, the rest will be empty. On the diagonal we plot the distance
        # matrices.
        fig, axes = plt.subplots(
            len(self.distance_matrix),
            len(self.distance_matrix),
            figsize=(len(self.distance_matrix) * 5, len(self.distance_matrix) * 5),
        )
        # Find maximal, non-inf value for the colorbar for the diagonal
        max_val = max(
            np.max(value[value != np.inf]) for value in self.distance_matrix.values()
        )
        # Calculate the pairwise relative difference between the distance matrices
        # save the relative difference and the minimal value for the colorbar regarding
        # the absolute value
        rel_diff = {}
        min_val = 0
        # For the lower triangle
        for i, (key_i, value_i) in enumerate(self.distance_matrix.items()):
            for j, (key_j, value_j) in enumerate(self.distance_matrix.items()):
                # Only plot the lower triangle
                if j <= i:
                    continue
                # Calculate the pairwise relative difference
                # Use np.inf if either value is np.inf or if the denominator is 0
                rel_diff[key_i, key_j] = np.where(
                    (value_i == np.inf)
                    | (value_j == np.inf)
                    | (value_j == 0)
                    | (value_i == 0),
                    np.inf,
                    (value_i - value_j) / value_j,
                )
                # Find the minimal value for the colorbar
                min_val = min(min_val, np.min(rel_diff[key_i, key_j]))

        # Plot distance matrices on diagonal axes and relative difference on the
        # lower triangle axes
        # Iterate over all combinations of keys, for the upper triangle make the axes
        # invisible
        # Only write labels on the left and bottom axes
        for i, (key_i, key_j) in enumerate(product(self.distance_matrix, repeat=2)):
            axe = axes[i // len(self.distance_matrix), i % len(self.distance_matrix)]

            # Make the upper triangle axes invisible
            if i // len(self.distance_matrix) < i % len(self.distance_matrix):
                axe.set_visible(False)
            # On the diagonal plot the distance matrices
            elif i // len(self.distance_matrix) == i % len(self.distance_matrix):
                # Use colormap viridis for the distance matrices
                dist_im = axe.imshow(
                    self.distance_matrix[key_i], vmin=0, vmax=max_val, cmap="viridis"
                )
                axe.set_title(f"$d_{key_i}(i, j)$")
                axe.set_aspect("equal")
            # On the lower triangle plot the pairwise relative difference
            else:
                # The relative differences are all negative, the colormap will go from
                # min_val to 0, a fitting colormap is RdYlGn
                diff_im = axe.imshow(
                    rel_diff[key_j, key_i], vmin=min_val, vmax=0, cmap="RdYlGn"
                )
                axe.set_title(
                    f"$\\frac{{d_{{{key_j}}}(i, j) - "
                    f"d_{{{key_i}}}(i, j)}}{{d_{{{key_i}}}(i, j)}}$"
                )
                axe.set_xlabel("Node $j$")
                axe.set_ylabel("Node $i$")
                axe.set_aspect("equal")
            # Only write labels on the left and bottom axes
            if i // len(self.distance_matrix) != len(self.distance_matrix) - 1:
                axe.set_xticklabels([])
            if i % len(self.distance_matrix) != 0:
                axe.set_yticklabels([])
        # Set the labels for all x and y axes
        for axe in axes[-1, :]:
            axe.set_xlabel("Node $j$")
        for axe in axes[:, 0]:
            axe.set_ylabel("Node $i$")

        # Plot the two colorbars on the right side of the figure
        # Colorbar for the diagonal
        fig.colorbar(dist_im, ax=axes, fraction=0.046, pad=0.04)
        # Colorbar for the lower triangle
        fig.colorbar(diff_im, ax=axes, fraction=0.046, pad=0.04)
        # Label colorbar
        unit = (
            "khops"
            if self.weight is None
            else "km"
            if self.weight == "length"
            else f"k{self.weight}"
        )
        dist_im.set_label(f"Distance [{unit}]")
        # Title above all subplots
        fig.suptitle(
            f"Pairwise relative difference between the distance matrices "
            f"{'(' + name + ')' if name else ''}"
        )

        return fig, axes

    @staticmethod
    def _has_pairwise_overlap(lists):
        """Return a boolean array indicating overlap between pairs of lists.

        Uses numpy arrays and vector broadcasting to speed up the calculation.
        For short lists using set operations is faster.

        Parameters
        ----------
        lists : list of lists
            The lists to check for pairwise overlap. Lists can be of different length.

        Raises
        ------
        ValueError
            If lists is not a list of lists.
        ValueError
            If lists is empty.

        Returns
        -------
        has_overlap : ndarray
            A boolean array indicating whether there is overlap between each pair of
            lists. has_overlap[i, j] is True if there is overlap between list i and
            list j, and False otherwise.

        """
        if not isinstance(lists, list) or not all(
            isinstance(lst, list) for lst in lists
        ):
            raise ValueError("The input must be a list of lists.")
        if not lists:
            raise ValueError("The input must not be the empty list.")

        # Convert lists to sets
        sets = [set(lst) for lst in lists]

        # Compute the pairwise intersection of the sets
        intersections = np.zeros((len(sets), len(sets)), dtype=np.int32)
        for i, _ in enumerate(sets):
            for j, _ in enumerate(sets):
                intersections[i, j] = len(sets[i] & sets[j])
                intersections[j, i] = intersections[i, j]

        # Compute the pairwise union of the sets
        unions = np.array([len(s) for s in sets]).reshape(-1, 1) + len(lists) - 1

        # Compute whether there is overlap between each pair of sets
        has_overlap = intersections > 0
        overlaps = intersections / unions
        np.fill_diagonal(overlaps, 0)
        has_overlap |= overlaps > 0

        return has_overlap

    def save(self, name):
        """Save the metric to a file.

        Will be saved as a pickle file at RESULTS_DIR/name.metrics.

        Parameters
        ----------
        name : str
            The name of the file to save the metric to.

        """

        metrics_path = path.join(RESULTS_DIR, name, name + ".metrics")
        # Check if metrics already exist
        if path.exists(metrics_path):
            logger.debug("Metrics already exist, overwriting %s", metrics_path)
        else:
            logger.debug("Saving metrics to %s", metrics_path)
        with open(metrics_path, "wb") as file:
            pickle.dump(self, file)

    @classmethod
    def load(cls, name):
        """Load a partitioning from a file.

        Parameters
        ----------
        path : str
            The path to the file to load the partitioning from.

        Returns
        -------
        partitioning : Partitioning
            The loaded partitioning.

        """

        metrics_path = path.join(RESULTS_DIR, name, name + ".metrics")
        logger.debug("Loading metrics from %s", metrics_path)
        with open(metrics_path, "rb") as file:
            metrics = pickle.load(file)

        return metrics
