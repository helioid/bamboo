from collections import defaultdict

from celery.task import task
from pandas import concat

from bamboo.core.aggregator import Aggregator
from bamboo.core.frame import BambooFrame, NonUniqueJoinError
from bamboo.core.parser import ParseError, Parser
from bamboo.lib.mongo import MONGO_RESERVED_KEYS
from bamboo.lib.async import call_async
from bamboo.lib.utils import split_groups


class Calculator(object):
    """Perform and store calculations and recalculations on update."""

    calcs_to_data = None
    dframe = None

    def __init__(self, dataset):
        self.dataset = dataset
        self.parser = Parser(dataset)

    def validate(self, formula, group_str):
        """Validate `formula` and `group_str` for calculator's dataset.

        Validate the formula and group string by attempting to get a row from
        the dframe for the dataset and then running parser validation on this
        row. Additionally, ensure that the groups in the group string are
        columns in the dataset.

        :param formula: The formula to validate.
        :param group_str: A string of a group or comma separated groups.

        :returns: The aggregation (or None) for the formula.
        """
        dframe = self.dataset.dframe(limit=1)
        row = dframe.irow(0) if len(dframe) else {}

        aggregation = self.parser.validate_formula(formula, row)

        if group_str:
            groups = split_groups(group_str)
            for group in groups:
                if not group in dframe.columns:
                    raise ParseError(
                        'Group %s not in dataset columns.' % group)

        return aggregation

    def calculate_column(self, formula, name, group_str=None):
        """Calculate a new column based on `formula` store as `name`.

        The new column is joined to `dframe` and stored in `self.dataset`.
        The `group_str` is only applicable to aggregations and groups for
        aggregations.

        .. note::

            This can result in race-conditions when:

            - deleting ``controllers.Datasets.DELETE``
            - updating ``controllers.Datasets.POST([dataset_id])``

            Therefore, perform these actions asychronously.

        :param formula: The formula parsed by `self.parser` and applied to
            `self.dframe`.
        :param name: The name of the new column or aggregate column.
        :param group_str: A string or columns to group on for aggregate
            calculations.
        """
        self._ensure_dframe()

        aggregation, new_columns = self.make_columns(formula, name)

        if aggregation:
            agg = Aggregator(self.dataset, self.dframe,
                             group_str, aggregation, name)
            agg.save(new_columns)
        else:
            self.dataset.replace_observations(self.dframe.join(new_columns[0]))

        # propagate calculation to any merged child datasets
        for merged_dataset in self.dataset.merged_datasets:
            merged_calculator = Calculator(merged_dataset)
            merged_calculator.propagate_column(self.dataset)

    def propagate_column(self, parent_dataset):
        """Propagate columns in `parent_dataset` to this dataset.

        This is used when there has been a new calculation added to
        a dataset and that new column needs to be propagated to all
        child (merged) datasets.

        :param parent_dataset: The dataset to propagate to `self.dataset`.
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
            merged_calculator.propagate_column(self.dataset)

    @task
    def calculate_updates(self, new_data, new_dframe_raw=None,
                          parent_dataset_id=None):
        """Update dataset with `new_data`.

        This can result in race-conditions when:

        - deleting ``controllers.Datasets.DELETE``
        - updating ``controllers.Datasets.POST([dataset_id])``

        Therefore, perform these actions asychronously.

        :param new_data: Data to update this dataset with.
        :param new_dframe_raw: DataFrame to update this dataset with.
        :param parent_dataset_id: If passed add ID as parent ID to column,
            default is None.
        """
        self._ensure_dframe()
        self._ensure_ready()

        labels_to_slugs = self.dataset.schema.labels_to_slugs

        if new_dframe_raw is None:
            new_dframe_raw = self.dframe_from_update(new_data, labels_to_slugs)

        self._check_update_is_valid(new_dframe_raw)

        new_dframe = new_dframe_raw.recognize_dates_from_schema(
            self.dataset.schema)

        new_dframe, aggregations = self._add_calcs_and_find_aggregations(
            new_dframe, labels_to_slugs)

        # set parent id if provided
        if parent_dataset_id:
            new_dframe = new_dframe.add_parent_column(parent_dataset_id)

        existing_dframe = self.dataset.dframe(keep_parent_ids=True)

        # merge the two dframes
        updated_dframe = concat([existing_dframe, new_dframe])

        # update (overwrite) the dataset with the new merged dframe
        self.dframe = self.dataset.replace_observations(
            updated_dframe, set_num_columns=False)
        self.dataset.clear_summary_stats()

        self._update_aggregate_datasets(aggregations, new_dframe)
        self._update_merged_datasets(new_data, labels_to_slugs)
        self._update_joined_datasets(new_dframe_raw)

    def _check_update_is_valid(self, new_dframe_raw):
        """Check if the update is valid.

        Check whether this is a right-hand side of any joins
        and deny the update if the update would produce an invalid
        join as a result.

        :raises: `NonUniqueJoinError` if update is illegal given joins of
            dataset.
        """
        if any([direction == 'left' for direction, _, on, __ in
                self.dataset.joined_datasets]):
            if on in new_dframe_raw.columns and on in self.dframe.columns:
                merged_join_column = concat(
                    [new_dframe_raw[on], self.dframe[on]])
                if len(merged_join_column) != merged_join_column.nunique():
                    raise NonUniqueJoinError(
                        'Cannot update. This is the right hand join and the'
                        'column "%s" will become non-unique.' % on)

    def make_columns(self, formula, name, dframe=None):
        """Parse formula into function and variables."""
        if dframe is None:
            dframe = self.dframe

        aggregation, functions = self.parser.parse_formula(formula)

        new_columns = []

        for function in functions:
            new_column = dframe.apply(
                function, axis=1, args=(self.parser.context, ))
            new_column.name = name
            new_columns.append(new_column)

        return aggregation, new_columns

    def _ensure_dframe(self):
        """Ensure `dframe` for the calculator's dataset is defined."""
        if self.dframe is None:
            self.dframe = self.dataset.dframe()

    def _ensure_ready(self):
        # dataset must not be pending
        if not self.dataset.is_ready:
            self.dataset.reload()
            raise self.calculate_updates.retry(countdown=1)

    def _add_calcs_and_find_aggregations(self, new_dframe, labels_to_slugs):
        aggregations = []
        calculations = self.dataset.calculations()

        for calculation in calculations:
            _, function =\
                self.parser.parse_formula(calculation.formula)
            new_column = new_dframe.apply(function[0], axis=1,
                                          args=(self.parser.context, ))
            potential_name = calculation.name

            if potential_name not in self.dframe.columns:
                if potential_name not in labels_to_slugs:
                    # it is an aggregate calculation, update after
                    aggregations.append(calculation)
                    continue
                else:
                    new_column.name = labels_to_slugs[potential_name]
            else:
                new_column.name = potential_name
            new_dframe = new_dframe.join(new_column)

        return new_dframe, aggregations

    def _update_merged_datasets(self, new_data, labels_to_slugs):
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
            call_async(merged_calculator.calculate_updates, merged_calculator,
                       slugified_data, parent_dataset_id=self.dataset.dataset_id)

    def _update_joined_datasets(self, new_dframe_raw):
        # update any joined datasets
        for direction, other_dataset, on, joined_dataset in\
                self.dataset.joined_datasets:
            if direction == 'left':
                if on in new_dframe_raw.columns:
                    # only proceed if on in new dframe
                    other_dframe = other_dataset.dframe(padded=True)

                    if len(set(new_dframe_raw[on]).intersection(
                            set(other_dframe[on]))):
                        # only proceed if new on value is in on column in lhs
                        merged_dframe = other_dframe.join_dataset(
                            self.dataset, on)
                        joined_dataset.replace_observations(merged_dframe)
            else:
                merged_dframe = new_dframe_raw

                if on in merged_dframe:
                    merged_dframe = new_dframe_raw.join_dataset(
                        other_dataset, on)

                joined_calculator = Calculator(joined_dataset)
                call_async(joined_calculator.calculate_updates,
                           joined_calculator, merged_dframe.to_jsondict(),
                           parent_dataset_id=self.dataset.dataset_id)

    def dframe_from_update(self, new_data, labels_to_slugs):
        """Make a single-row dataframe for the additional data to add."""
        self._ensure_dframe()

        if not isinstance(new_data, list):
            new_data = [new_data]

        filtered_data = []
        columns = self.dframe.columns
        dframe_empty = not len(columns)

        if dframe_empty:
            columns = self.dataset.schema.keys()

        for row in new_data:
            filtered_row = dict()
            for col, val in row.iteritems():
                # special case for reserved keys (e.g. _id)
                if col in MONGO_RESERVED_KEYS:
                    if (not len(columns) or col in columns) and\
                            col not in filtered_row.keys():
                        filtered_row[col] = val
                else:
                    # if col is a label take slug, if it's a slug take col
                    slug = labels_to_slugs.get(
                        col, col if col in labels_to_slugs.values() else None)

                    # if slug is valid of there is an empty dframe
                    if (slug or col in labels_to_slugs.keys()) and (
                            dframe_empty or slug in columns):
                        filtered_row[slug] = self.dataset.schema.convert_type(
                            slug, val)

            filtered_data.append(filtered_row)

        return BambooFrame(filtered_data)

    def _update_aggregate_datasets(self, calculations, new_dframe):
        if not self.calcs_to_data:
            self._create_calculations_to_groups_and_datasets(calculations)

        for formula, slug, group, dataset in self.calcs_to_data:
            self._update_aggregate_dataset(formula, new_dframe, slug, group,
                                           dataset)

    def _update_aggregate_dataset(self, formula, new_dframe, name, group,
                                  agg_dataset):
        """Update the aggregated dataset built for `self` with `calculation`.

        Proceed with the following steps:

            - delete the rows in this dataset from the parent
            - recalculate aggregated dataframe from aggregation
            - update aggregated dataset with new dataframe and add parent id
            - recur on all merged datasets descending from the aggregated
              dataset

        :param formula: The formula to execute.
        :param new_dframe: The DataFrame to aggregate on.
        :param name: The name of the aggregation.
        :param group: A column or columns to group on.
        :type group: String, list of strings, or None.
        :param agg_dataset: The DataSet to store the aggregation in.
        """
        aggregation, new_columns = self.make_columns(
            formula, name, new_dframe)

        agg = Aggregator(self.dataset, self.dframe,
                         group, aggregation, name)
        new_agg_dframe = agg.update(agg_dataset, self, formula, new_columns)

        # jsondict from new dframe
        new_data = new_agg_dframe.to_jsondict()

        for merged_dataset in agg_dataset.merged_datasets:
            # remove rows in child from this merged dataset
            merged_dataset.remove_parent_observations(
                agg_dataset.dataset_id)

            # calculate updates on the child
            merged_calculator = Calculator(merged_dataset)
            call_async(merged_calculator.calculate_updates, merged_calculator,
                       new_data, parent_dataset_id=agg_dataset.dataset_id)

    def _create_calculations_to_groups_and_datasets(self, calculations):
        """Create list of groups and calculations."""
        self.calcs_to_data = defaultdict(list)
        names_to_formulas = {
            calc.name: calc.formula for calc in calculations
        }
        calculations = set([calc.name for calc in calculations])

        for group, dataset in self.dataset.aggregated_datasets.items():
            labels_to_slugs = dataset.schema.labels_to_slugs

            for calc in list(
                    set(labels_to_slugs.keys()).intersection(calculations)):
                self.calcs_to_data[calc].append(
                    (names_to_formulas[calc],
                     labels_to_slugs[calc], group, dataset))

        self.calcs_to_data = [
            item for sublist in self.calcs_to_data.values() for item in sublist
        ]

    def __getstate__(self):
        """Get state for pickle."""
        return [self.dataset, self.parser]

    def __setstate__(self, state):
        self.dataset, self.parser = state
