from collections import defaultdict

from celery.task import task
from pandas import concat

from bamboo.core.aggregator import Aggregator
from bamboo.core.frame import BambooFrame
from bamboo.core.parser import ParseError, Parser
from bamboo.lib.datetools import recognize_dates, recognize_dates_from_schema
from bamboo.lib.mongo import MONGO_RESERVED_KEYS
from bamboo.lib.utils import call_async, split_groups


class Calculator(object):

    labels_to_slugs_and_groups = None

    def __init__(self, dataset):
        self.dataset = dataset
        self.dframe = dataset.dframe()
        self.parser = Parser(dataset)

    def validate(self, formula, group_str):
        # attempt to get a row from the dataframe
        try:
            row = self.dframe.irow(0)
        except IndexError, err:
            row = {}

        self.parser.validate_formula(formula, row)

        if group_str:
            groups = split_groups(group_str)
            for group in groups:
                if not group in self.dframe.columns:
                    raise ParseError(
                        'Group %s not in dataset columns.' % group)

    @task
    def calculate_column(self, formula, name, group_str=None):
        """
        Calculate a new column based on *formula* store as *name*.
        The *fomula* is parsed by *self.parser* and applied to *self.dframe*.
        The new column is joined to *dframe* and stored in *self.dataset*.
        The *group_str* is only applicable to aggregations and groups for
        aggregations.

        This can result in race-conditions when:

        - deleting ``controllers.Datasets.DELETE``
        - updating ``controllers.Datasets.POST([dataset_id])``

        Therefore, perform these actions asychronously.
        """

        aggregation, new_columns = self._make_columns(formula, name)

        if aggregation:
            agg = Aggregator(self.dataset, self.dframe, new_columns,
                             group_str, aggregation, name)
            agg.save()
        else:
            self.dataset.replace_observations(self.dframe.join(new_columns[0]))

        # propagate calculation to any merged child datasets
        for merged_dataset in self.dataset.merged_datasets:
            merged_calculator = Calculator(merged_dataset)
            merged_calculator._propagate_column(self.dataset)

    def _propagate_column(self, parent_dataset):
        """
        This is called when there has been a new calculation added to
        a dataset and that new column needs to be propagated to all
        child (merged) datasets.
        """
        # delete the rows in this dataset from the parent
        self.dataset.remove_parent_observations(parent_dataset.dataset_id)
        # get this dataset without the out-of-date parent rows
        dframe = self.dataset.dframe(keep_parent_ids=True)

        # create new dframe from the upated parent and add parent id
        parent_dframe = parent_dataset.dframe().add_parent_column(
            parent_dataset.dataset_id)

        # merge this new dframe with the existing dframe
        updated_dframe = concat([dframe, parent_dframe])

        # save new dframe (updates schema)
        self.dataset.replace_observations(updated_dframe)
        self.dataset.clear_summary_stats()

        # recur
        for merged_dataset in self.dataset.merged_datasets:
            merged_calculator = Calculator(merged_dataset)
            merged_calculator._propagate_column(self.dataset)

    @task
    def calculate_updates(self, new_data, parent_dataset_id=None):
        """
        Update dataset with *new_data*.

        This can result in race-conditions when:

        - deleting ``controllers.Datasets.DELETE``
        - updating ``controllers.Datasets.POST([dataset_id])``

        Therefore, perform these actions asychronously.
        """
        calculations = self.dataset.calculations()
        labels_to_slugs = self.dataset.build_labels_to_slugs()
        new_dframe = self._dframe_from_update(new_data, labels_to_slugs)

        aggregate_calculations = []

        for calculation in calculations:
            aggregation, function =\
                self.parser.parse_formula(calculation.formula)
            new_column = new_dframe.apply(function[0], axis=1,
                                          args=(self.parser.context, ))
            potential_name = calculation.name
            if potential_name not in self.dframe.columns:
                if potential_name not in labels_to_slugs:
                    # it is an aggregate calculation, update after
                    aggregate_calculations.append(calculation)
                    continue
                else:
                    new_column.name = labels_to_slugs[potential_name]
            else:
                new_column.name = potential_name
            new_dframe = new_dframe.join(new_column)

        # set parent id if provided
        if parent_dataset_id:
            new_dframe = new_dframe.add_parent_column(parent_dataset_id)

        existing_dframe = self.dataset.dframe(keep_parent_ids=True)

        # merge the two
        updated_dframe = concat([existing_dframe, new_dframe])

        # update (overwrite) the dataset with the new merged version
        self.dframe = self.dataset.replace_observations(updated_dframe)
        self.dataset.clear_summary_stats()

        self._update_aggregate_datasets(aggregate_calculations, new_dframe)

        # store slugs as labels for child datasets
        slugified_data = []
        if not isinstance(new_data, list):
            new_data = [new_data]
        for row in new_data:
            for key, value in row.iteritems():
                if labels_to_slugs.get(key) and key not in MONGO_RESERVED_KEYS:
                    del row[key]
                    row[labels_to_slugs[key]] = value
            slugified_data.append(row)

        # update the merged datasets with new_dframe
        for merged_dataset in self.dataset.merged_datasets:
            merged_calculator = Calculator(merged_dataset)
            call_async(merged_calculator.calculate_updates,
                       merged_dataset, merged_calculator,
                       slugified_data, self.dataset.dataset_id)

    def _make_columns(self, formula, name, dframe=None):
        """
        Parse formula into function and variables
        """
        if dframe is None:
            dframe = self.dframe

        aggregation, functions = self.parser.parse_formula(formula)

        new_columns = []
        for function in functions:
            new_column = dframe.apply(
                function, axis=1, args=(self.parser.context, ))
            new_columns.append(new_column)

        new_columns[0].name = name

        return aggregation, new_columns

    def _dframe_from_update(self, new_data, labels_to_slugs):
        """
        Make a single-row dataframe for the additional data to add
        """
        if not isinstance(new_data, list):
            new_data = [new_data]

        filtered_data = []
        for row in new_data:
            filtered_row = dict()
            for col, val in row.iteritems():
                if labels_to_slugs.get(col, None) in self.dframe.columns:
                    filtered_row[labels_to_slugs[col]] = val
                # special case for reserved keys (e.g. _id)
                if col in MONGO_RESERVED_KEYS and\
                    col in self.dframe.columns and\
                        col not in filtered_row.keys():
                    filtered_row[col] = val
            filtered_data.append(filtered_row)
        return recognize_dates_from_schema(self.dataset,
                                           BambooFrame(filtered_data))

    def _update_aggregate_datasets(self, aggregate_calculations, new_dframe):
        for calculation in aggregate_calculations:
            self._update_aggregate_dataset(calculation, new_dframe)

    def _update_aggregate_dataset(self, calculation, new_dframe):
        """
        Update the aggregated dataset built for *self* with *calculation*.

        - delete the rows in this dataset from the parent
        - recalculate aggregated dataframe from aggregation
        - update aggregated dataset with new dataframe and add parent id
        - recur on all merged datasets descending from the aggregated dataset

        """
        name, group, agg_dataset = self._agg_data_for_label(calculation.name)
        aggregation, new_columns = self._make_columns(
            calculation.formula, name, new_dframe)
        agg_dframe = agg_dataset.dframe()

        agg = Aggregator(agg_dataset, agg_dframe, new_columns,
                         group, aggregation, name)
        new_agg_dframe = agg.update(self.dataset.dataset_id)

        # jsondict from new dframe
        new_data = new_agg_dframe.to_jsondict()

        for merged_dataset in agg_dataset.merged_datasets:
            # remove rows in child from this merged dataset
            merged_dataset.remove_parent_observations(
                agg_dataset.dataset_id)

            # calculate updates on the child
            merged_calculator = Calculator(merged_dataset)
            call_async(merged_calculator.calculate_updates,
                       merged_dataset, merged_calculator, new_data,
                       self.dataset.dataset_id)

    def _agg_data_for_label(self, label):
        if not self.labels_to_slugs_and_groups:
            self._create_labels_to_slugs_and_groups()
        return self.labels_to_slugs_and_groups.get(label)

    def _create_labels_to_slugs_and_groups(self):
        """
        Extract info from linked datasets
        """
        self.labels_to_slugs_and_groups = dict()
        for group, dataset in self.dataset.aggregated_datasets.items():
            for label, slug in dataset.build_labels_to_slugs().items():
                self.labels_to_slugs_and_groups[label] = (
                    slug, group, dataset)
