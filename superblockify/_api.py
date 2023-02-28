"""Expose most common parts of public API directly in `superblockify.`
namespace."""

# pylint: disable=unused-import
from .attribute import new_edge_attribute_by_function
from .partitioning import DummyPartitioner
from .partitioning import BearingPartitioner
from .plot import paint_streets, plot_by_attribute, plot_road_type_for
