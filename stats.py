import numpy as np
from pandas import pivot_table

def parse_value_counts(data):
    value_counts = data.value_counts()
    if value_counts.get('null'):
        return value_counts.drop(['null'])
    return value_counts


def summary_stats(dtype, data):
    return {
        np.object_: parse_value_counts(data),
        np.float64: data.describe(),
    }.get(dtype.type, None)
    return None

def calculate_group(df, dtypes, group):
    cols = filter(lambda e: dtypes[e].type is not np.object_, df.columns)
    return pivot_table(df, rows=[group], cols=cols, aggfunc=np.sum)
