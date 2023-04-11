"""Load this module to fetch test data needed for certain tests."""
from ast import literal_eval
from configparser import ConfigParser
from os.path import join, dirname

import osmnx as ox

from superblockify.utils import load_graph_from_place

# turn response caching off as this only loads graphs to files
ox.config(use_cache=False, log_console=True)

config = ConfigParser()
config.read(join(dirname(__file__), "..", "..", "config.ini"))

if __name__ == "__main__":
    for place in literal_eval(config["tests"]["places_small"]) + literal_eval(
        config["tests"]["places_general"]
    ):
        load_graph_from_place(
            f"./tests/test_data/cities/{place[0]}.graphml",
            place[1],
            network_type="drive",
        )
