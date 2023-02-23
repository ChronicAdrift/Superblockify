"""Plotting functions."""
import logging

import networkx as nx
import osmnx as ox
from matplotlib import patches
from matplotlib import pyplot as plt
from numpy import amin, amax

from superblockify import attribute

logger = logging.getLogger("superblockify")


def paint_streets(graph, cmap="hsv", **pg_kwargs):
    """Plot a graph with (cyclic) colormap related to edge direction.

    Color will be chosen based on edge bearing, cyclic in 90 degree.
    Function is a wrapper around `osmnx.plot_graph`.

    Parameters
    ----------
    graph : networkx.Graph
        Input graph
    cmap : string, optional
        name of a matplotlib colormap
    pg_kwargs
        keyword arguments to pass to `osmnx.plot_graph`.

    Returns
    -------
    fig, ax : tuple
        matplotlib figure, axis

    Examples
    --------
    _See example in `scripts/TestingNotebooks/20221122-painting_grids.py`._

    """

    # Calculate bearings if no edge has `bearing` attribute.
    if not bool(nx.get_edge_attributes(graph, "bearing")):
        graph = ox.add_edge_bearings(graph)

    # Write attribute where bearings are baked down modulo 90 degrees.
    attribute.new_edge_attribute_by_function(
        graph, lambda bear: bear % 90, "bearing", "bearing_90"
    )

    return plot_by_attribute(
        graph, "bearing_90", attr_types="numerical", cmap=cmap, **pg_kwargs
    )


def plot_by_attribute(
    graph,
    attr,
    attr_types="numerical",
    cmap="hsv",
    edge_linewidth=1,
    node_alpha=0,
    minmax_val=None,
    **pg_kwargs,
):  # pylint: disable=too-many-arguments
    """Plot a graph based on an edge attribute and colormap.

    Color will be chosen based on the specified edge attribute passed to a colormap.
    Function is a direct wrapper around `osmnx.plot_graph`.

    Parameters
    ----------
    graph : networkx.MultiDiGraph
        Input graph
    attr : string
        Graph's attribute to select colors by
    attr_types : string, optional
        Type of the attribute to be plotted, can be 'numerical' or 'categorical'
    cmap : string, optional
        Name of a matplotlib colormap
    edge_linewidth : float, optional
        Width of the edges' lines
    node_alpha : float, optional
        Opacity of the nodes
    edge_color : None, optional
        Do not pass this attribute, as it is set by the bearing direction.
    minmax_val : tuple, optional
        Tuple of (min, max) values of the attribute to be plotted
        (default: min and max of attr)
    pg_kwargs
        Keyword arguments to pass to `osmnx.plot_graph`.

    Raises
    ------
    ValueError
        If edge_color was set to anything but None.
    ValueError
        If `edge_linewidth` and `node_size` both <= 0, otherwise the plot will be empty.

    Returns
    -------
    fig, axe : tuple
        matplotlib figure, axis

    """

    if ("edge_color" in pg_kwargs) and (pg_kwargs["edge_color"] is not None):
        raise ValueError(
            f"The `edge_color` attribute was set to {pg_kwargs['edge_color']}, "
            f"it will be overwritten by the colors determined with the "
            f"bearings and colormap."
        )

    # Choose the color for each edge based on the edge's attribute value,
    # if `None`, set to gray.
    colormap = plt.get_cmap(cmap)

    # Make list of edge colors, order is the same as in graph.edges()
    e_c = list(
        make_edge_color_list(
            graph, attr, colormap, attr_types=attr_types, minmax_val=minmax_val
        )
    )

    # Print list of unique colors in the colormap, with a set comprehension
    logger.debug(
        "Unique colors in the colormap %s: %s",
        cmap,
        {tuple(c) for c in e_c},
    )

    # Plot graph with osmnx's function, pass further attributes
    return ox.plot_graph(
        graph,
        node_alpha=node_alpha,
        edge_color=e_c,
        edge_linewidth=edge_linewidth,
        **pg_kwargs,
    )


def make_edge_color_list(
    graph,
    attr,
    cmap,
    attr_types="numerical",
    minmax_val=None,
    none_color=(0.5, 0.5, 0.5, 1),
):  # pylint: disable=too-many-arguments
    """Make a list of edge colors based on an edge attribute and colormap.

    Color will be chosen based on the specified edge attribute passed to a colormap.

    Parameters
    ----------
    graph : networkx.MultiDiGraph
        Input graph
    attr : string
        Graph's attribute to select colors by
    attr_types : string, optional
        Type of the attribute to be plotted, can be 'numerical' or 'categorical'
    cmap : matplotlib.colors.Colormap
        Colormap to use for the plot
    minmax_val : tuple, optional
        If `attr_types` is 'numerical', tuple of (min, max) values of the attribute
        to be plotted (default: min and max of attr)
    none_color : tuple, optional
        Color to use for edges with `None` attribute value

    Raises
    ------
    ValueError
        If `attr_types` is not 'numerical' or 'categorical'
    ValueError
        If `attr_types` is 'categorical' and `minmax_val` is not None

    Returns
    -------
    list
        List of edge colors, order is the same as in graph.edges()

    """

    if attr_types == "categorical" and minmax_val is not None:
        raise ValueError(
            f"The `minmax_val` attribute was set to {minmax_val}, "
            f"it should be None."
        )

    if attr_types == "numerical":
        minmax_val = determine_minmax_val(graph, minmax_val, attr)
        return [
            cmap((attr_val - minmax_val[0]) / (minmax_val[1] - minmax_val[0]))
            if attr_val is not None
            else none_color
            for u, v, k, attr_val in graph.edges(keys=True, data=attr)
        ]
    if attr_types == "categorical":
        # Enumerate through the unique values of the attribute
        # and assign a color to each value, `None` will be assigned to `none_color`.
        unique_vals = set(
            attr_val for u, v, k, attr_val in graph.edges(keys=True, data=attr)
        )

        # To sort the values, remove None from the set, sort the values,
        # and add None back to the set.
        unique_vals.discard(None)
        unique_vals = list(unique_vals)
        try:
            unique_vals = sorted(unique_vals)
        except TypeError:
            # If the values are not sortable, just leave them as they are.
            logger.debug(
                "The values of the attribute %s are not sortable, "
                "the order of the colors in the colormap will be random.",
                attr,
            )
        finally:
            unique_vals.append(None)

        return [
            cmap(unique_vals.index(attr_val) / (len(unique_vals) - 1))
            if attr_val is not None
            else none_color
            for u, v, k, attr_val in graph.edges(keys=True, data=attr)
        ]
    # If attr_types is not 'numerical' or 'categorical', raise an error
    raise ValueError(
        f"The `attr_types` attribute was set to {attr_types}, "
        f"it should be 'numerical' or 'categorical'."
    )


def determine_minmax_val(graph, minmax_val, attr):
    """Determine the min and max values of an attribute in a graph.

    This function is used to determine the min and max values of an attribute.
    If `minmax_val` is None, the min and max values of the attribute in the graph
    are used. If `minmax_val` is a tuple of length 2, the values are used as
    min and max values. If `minmax_val` is a tuple of length 2, but the first
    value is larger than the second, a ValueError is raised.
    If only one value in the tuple is given, the other value is set accordingly.

    Parameters
    ----------
    graph : networkx.MultiDiGraph
        Input graph
    minmax_val : tuple, None
        Tuple of (min, max) values of the attribute to be plotted or None
    attr : string
        Graph's attribute to select min and max values by

    Raises
    ------
    ValueError
        If `minmax_val` is not a tuple of length 2 or None.
    ValueError
        If `minmax_val[0]` is not smaller than `minmax_val[1]`.

    """

    if minmax_val is not None and (
        not isinstance(minmax_val, tuple) or len(minmax_val) != 2
    ):
        raise ValueError(
            f"The `minmax_val` attribute was set to {minmax_val}, "
            f"it should be a tuple of length 2 or None."
        )

    # Determine min and max values of the attribute
    logger.debug("Given minmax_val for attribute %s: %s", attr, minmax_val)
    if minmax_val is None or minmax_val[0] is None or minmax_val[1] is None:
        # Min and max of the attribute, ignoring `None` values
        minmax = (
            amin([v for v in nx.get_edge_attributes(graph, attr).values() if v]),
            amax([v for v in nx.get_edge_attributes(graph, attr).values() if v]),
        )
        if minmax_val is None:
            minmax_val = minmax
        elif minmax_val[0] is None:
            minmax_val = (minmax[0], minmax_val[1])
        else:
            minmax_val = (minmax_val[0], minmax[1])
        logger.debug("Determined minmax_val for attribute %s: %s", attr, minmax_val)
    if minmax_val[0] >= minmax_val[1]:
        raise ValueError(
            f"The `minmax_val` attribute is {minmax_val}, "
            f"but the first value must be smaller than the second."
        )
    return minmax_val


# Ignore too-many-arguments, as we want to pass all arguments to the function
def plot_component_size(
    graph,
    attr,
    component_size,
    component_values,
    size_measure_label,
    ignore=None,
    title=None,
    cmap="hsv",
    minmax_val=None,
    num_component_log_scale=True,
    show_legend=None,
    **kwargs,
):  # pylint: disable=too-many-arguments, too-many-locals
    """Plot the distribution of component sizes for each partition value.

    x-axis: values of the partition
    y-axis: size of the component (e.g. number of edges, nodes or length)
    color: value of the partition

    Parameters
    ----------
    graph : networkx.MultiDiGraph
        Input graph
    attr : string
        Graph's attribute to select colormap min and max values by
        if `minmax_val` is incomplete
    component_size : list
        Number of edges in each component
    component_values : list
        Value of the partition for each component
    size_measure_label : str
        Label of the size measure (e.g. "Number of edges", "Number of nodes",
        "Length [m]")
    ignore : list, optional
        List of values to ignore, plot in gray. If None, no values are ignored.
    title : str, optional
        Title of the plot
    cmap : string, optional
        Name of a matplotlib colormap
    minmax_val : tuple, optional
        Tuple of (min, max) values of the attribute to be plotted
        (default: min and max of attr)
    num_component_log_scale : bool, optional
        If True, the y-axis is plotted on a log scale
    show_legend : bool, optional
        If True, the legend is shown. If None, the legend is shown if the unique
        values of the partition are less than 23.
    kwargs
        Keyword arguments to pass to `matplotlib.pyplot.plot`.

    Returns
    -------
    fig, axe : tuple
        matplotlib figure, axis
    """

    fig, axe = plt.subplots()

    # Choose color of each value
    minmax_val = determine_minmax_val(graph, minmax_val, attr)
    colormap = plt.get_cmap(cmap)

    # Plot
    logger.debug("Plotting component/partition sizes for %s.", title)
    # Labelling
    axe.set_xlabel(attr)
    axe.set_ylabel(size_measure_label)
    if title is not None:
        axe.set_title(f"Component size of {title}")

    # Scaling and grid
    if num_component_log_scale:
        axe.set_yscale("log")
    axe.grid(True)
    plt.xticks([0, 15, 30, 45, 60, 75, 90])

    # Make legend with unique colors
    sorted_unique_values = sorted(set(component_values))

    # Show legend if `show_legend` is True, not when it is False,
    # and if it is None, only if the number of unique values is less than 23
    if show_legend or (show_legend is None and len(sorted_unique_values) < 23):
        sorted_unique_colors = [
            colormap((v - minmax_val[0]) / (minmax_val[1] - minmax_val[0]))
            for v in sorted_unique_values
        ]
        # Place legend on the outside right without cutting off the plot
        axe.legend(
            handles=[
                patches.Patch(
                    color=sorted_unique_colors[i], label=sorted_unique_values[i]
                )
                for i in range(len(sorted_unique_values))
            ],
            fontsize="small",
            bbox_to_anchor=(1.05, 1),
            loc="upper left",
            borderaxespad=0.0,
        )
        plt.tight_layout()

    # Scatter plot
    axe.scatter(
        component_values,
        component_size,
        c=[
            colormap((v - minmax_val[0]) / (minmax_val[1] - minmax_val[0]))
            if i is False
            else "gray"
            for v, i in zip(component_values, ignore)
        ]
        if ignore is not None
        else [
            colormap((v - minmax_val[0]) / (minmax_val[1] - minmax_val[0]))
            for v in component_values
        ],
        alpha=0.5,
        zorder=2,
        **kwargs,
    )

    return fig, axe


def plot_distance_distributions(
    dist_matrix, dist_title, coords, coord_title, labels, distance_unit="km"
):  # pylint: disable=too-many-arguments
    """Plot the distributions of the euclidean distances and coordinates.

    Parameters
    ----------
    dist_matrix : ndarray
        The distance matrix for the partitioning. dist_matrix[i, j] is the euclidean
        distance between node i and node j.
    dist_title : str
        The title of the histogram of the euclidean distances.
    coords : tuple
        The coordinates of the nodes. coords[0] is the x-coordinates, coords[1] is
        the y-coordinates. Can be either angular or euclidean coordinates.
    coord_title : str
        The title of the scatter plot of the coordinates.
    labels : tuple
        The labels of the coordinates. labels[0] is the label of the x-coordinate,
        labels[1] is the label of the y-coordinate.
    distance_unit : str, optional

    """
    _, axe = plt.subplots(1, 2, figsize=(10, 5))
    # Plot distribution of distances
    axe[0].hist(dist_matrix.flatten() / 1000, bins=100)
    axe[0].set_title(dist_title)
    axe[0].set_xlabel(f"Distance [{distance_unit}]")
    axe[0].set_ylabel("Count")
    # Plot scatter plot of lat/lon, aspect ratio should be 1:1
    axe[1].set_aspect("equal")
    axe[1].scatter(coords[0], coords[1], alpha=0.5, s=1)
    axe[1].set_title(coord_title)
    axe[1].set_xlabel(labels[0])
    axe[1].set_ylabel(labels[1])
    plt.tight_layout()
    plt.show()
