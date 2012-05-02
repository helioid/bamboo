import json
from urllib2 import HTTPError

from pandas import read_csv

from lib.utils import mongo_to_json, open_data_file
from models.dataset import Dataset
from models.observation import Observation


class Datasets(object):
    'Datasets controller'

    def __init__(self):
        pass

    exposed = True

    def DELETE(self, id):
        """
        Delete observations (i.e. the dataset) with hash 'id' from mongo
        """
        dataset = Dataset.find_one(id)
        if dataset:
            Dataset.delete(id)
            Observation.delete(dataset)
            return 'deleted dataset: %s' % id
        return 'id not found'

    def GET(self, id, query=None):
        """
        Return data set for hash 'id' in format 'format'.
        Execute query 'query' in mongo if passed.
        """
        dataset = Dataset.find_one(id)
        if dataset:
            return mongo_to_json(Observation.find(dataset, query))
        return 'id not found'

    def POST(self, url=None):
        """
        Read data from URL 'url'.
        If URL is not provided and data is provided, read posted data 'data'.
        """
        _file = open_data_file(url)
        if not _file:
            # could not get a file handle
            return
        try:
            dframe = read_csv(_file, na_values=['n/a'])
        except (IOError, HTTPError):
            return # error reading file/url
        digest = Dataset.find_or_create(dframe, url=url)
        return json.dumps({'id': digest})
