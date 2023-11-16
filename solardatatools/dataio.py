# -*- coding: utf-8 -*-
""" Data IO Module

This module contains functions for obtaining data from various sources.

"""
from solardatatools.time_axis_manipulation import (
    standardize_time_axis,
    fix_daylight_savings_with_known_tz,
)
from solardatatools.utilities import progress

from time import time
from io import StringIO
import os
import json
import requests
import numpy as np
import pandas as pd
from typing import Callable, TypedDict, Any, Tuple, Dict
from functools import wraps
from datetime import datetime


class SSHParams(TypedDict):
    ssh_address_or_host: tuple[str, int]
    ssh_username: str
    ssh_private_key: str
    remote_bind_address: tuple[str, int]


class DBConnectionParams(TypedDict):
    database: str
    user: str
    password: str
    host: str
    port: int


def get_pvdaq_data(sysid=2, api_key="DEMO_KEY", year=2011, delim=",", standardize=True):
    """
    This fuction queries one or more years of raw PV system data from NREL's PVDAQ data service:
            https://maps.nrel.gov/pvdaq/
    """
    # Force year to be a list of integers
    ti = time()
    try:
        year = int(year)
    except TypeError:
        year = [int(yr) for yr in year]
    else:
        year = [year]
    # Each year must queries separately, so iterate over the years and generate a list of dataframes.
    df_list = []
    it = 0
    for yr in year:
        progress(it, len(year), "querying year {}".format(year[it]))
        req_params = {"api_key": api_key, "system_id": sysid, "year": yr}
        base_url = "https://developer.nrel.gov/api/pvdaq/v3/data_file?"
        param_list = [str(item[0]) + "=" + str(item[1]) for item in req_params.items()]
        req_url = base_url + "&".join(param_list)
        response = requests.get(req_url)
        if int(response.status_code) != 200:
            print("\n error: ", response.status_code)
            return
        df = pd.read_csv(StringIO(response.text), delimiter=delim)
        df_list.append(df)
        it += 1
    tf = time()
    progress(it, len(year), "queries complete in {:.1f} seconds       ".format(tf - ti))
    # concatenate the list of yearly data frames
    df = pd.concat(df_list, axis=0, sort=True)
    if standardize:
        print("\n")
        df, _ = standardize_time_axis(df, datetimekey="Date-Time", timeindex=False)
    return df


def load_pvo_data(
    file_index=None,
    id_num=None,
    location="s3://pv.insight.nrel/PVO/",
    metadata_fn="sys_meta.csv",
    data_fn_pattern="PVOutput/{}.csv",
    index_col=0,
    parse_dates=[0],
    usecols=[1, 3],
    fix_dst=True,
    tz_column="TimeZone",
    id_column="ID",
    verbose=True,
):
    """
    Wrapper function for loading data from NREL partnership. This data is in a
    secure, private S3 bucket for use by the GISMo team only. However, the
    function can be used to load any data that is a collection of CSV files
    with a single metadata file. The metadata file contains a sequential file
    index as well as a unique system ID number for each site. Either of these
    may be set by the user to retreive data, but the ID number will take
    precedent if both are provided. The data files are assumed to be uniquely
    identified by the system ID number. In addition, the metadata file contains
    a column with time zone information for fixing daylight savings time.

    :param file_index: the sequential index number of the system
    :param id_num: the system ID number (non-sequential)
    :param location: string identifying the directory containing the data
    :param metadata_fn: the location of the metadata file
    :param data_fn_pattern: the pattern of data file identification
    :param index_col: the column containing the index (see: pandas.read_csv)
    :param parse_dates: list of columns to parse dates (see: pandas.read_csv)
    :param usecols: columns to load from file (see: pandas.read_csv)
    :param fix_dst: boolean, if true, use provided timezone information to
        correct for daylight savings time in data
    :param tz_column: the column name in the metadata file that contains the
        timezone information
    :param id_column: the column name in the metadata file that contains the
        unique system ID information
    :param verbose: boolean, print information about retreived file
    :return: pandas dataframe containing system power data
    """
    meta = pd.read_csv(location + metadata_fn)
    if id_num is None:
        id_num = meta[id_column][file_index]
    else:
        file_index = meta[meta[id_column] == id_num].index[0]
    df = pd.read_csv(
        location + data_fn_pattern.format(id_num),
        index_col=index_col,
        parse_dates=parse_dates,
        usecols=usecols,
    )
    if fix_dst:
        tz = meta[tz_column][file_index]
        fix_daylight_savings_with_known_tz(df, tz=tz, inplace=True)
    if verbose:
        print("index: {}; system ID: {}".format(file_index, id_num))
    return df


def load_cassandra_data(
    siteid,
    column="ac_power",
    sensor=None,
    tmin=None,
    tmax=None,
    limit=None,
    cluster_ip=None,
    verbose=True,
):
    try:
        from cassandra.cluster import Cluster
    except ImportError:
        print(
            "Please install cassandra-driver in your Python environment to use this function"
        )
        return
    ti = time()
    if cluster_ip is None:
        home = os.path.expanduser("~")
        cluster_location_file = home + "/.aws/cassandra_cluster"
        try:
            with open(cluster_location_file) as f:
                cluster_ip = f.readline().strip("\n")
        except FileNotFoundError:
            msg = "Please put text file containing cluster IP address in "
            msg += "~/.aws/cassander_cluster or provide your own IP address"
            print(msg)
            return
    cluster = Cluster([cluster_ip])
    session = cluster.connect("measurements")
    cql = """
        select site, meas_name, ts, sensor, meas_val_f
        from measurement_raw
        where site = '{}'
            and meas_name = '{}'
    """.format(
        siteid, column
    )
    ts_constraint = np.logical_or(tmin is not None, tmax is not None)
    if tmin is not None:
        cql += "and ts > '{}'\n".format(tmin)
    if tmax is not None:
        cql += "and ts < '{}'\n".format(tmax)
    if sensor is not None and ts_constraint:
        cql += "and sensor = '{}'\n".format(sensor)
    elif sensor is not None and not ts_constraint:
        cql += "and ts > '2000-01-01'\n"
        cql += "and sensor = '{}'\n".format(sensor)
    if limit is not None:
        cql += "limit {}".format(np.int(limit))
    cql += ";"
    rows = session.execute(cql)
    df = pd.DataFrame(list(rows))
    df.replace(-999999.0, np.NaN, inplace=True)
    tf = time()
    if verbose:
        print("Query of {} rows complete in {:.2f} seconds".format(len(df), tf - ti))
    return df


def load_constellation_data(
    file_id,
    location="s3://pv.insight.misc/pv_fleets/",
    data_fn_pattern="{}_20201006_composite.csv",
    index_col=0,
    parse_dates=[0],
    json_file=False,
):
    df = pd.read_csv(
        location + data_fn_pattern.format(file_id),
        index_col=index_col,
        parse_dates=parse_dates,
    )

    if json_file:
        try:
            from smart_open import smart_open
        except ImportError:
            print(
                "Please install smart_open in your Python environment to use this function"
            )
            return df, None

        for line in smart_open(location + str(file_id) + "_system_details.json", "rb"):
            file_json = json.loads(line)
            file_json
        return df, file_json
    return df


def load_redshift_data(
    ssh_params: SSHParams,
    redshift_params: DBConnectionParams,
    siteid: str,
    column: str = "ac_power",
    sensor: int | list[int] | None = None,
    tmin: datetime | None = None,
    tmax: datetime | None = None,
    limit: int | None = None,
    verbose: bool = False,
) -> pd.DataFrame:
    """Loads data based on a site id from a Redshift database into a Pandas DataFrame using an SSH tunnel

    Parameters
    ----------
        ssh_params : SSHParams
            SSH connection parameters
        redshift_params : DBConnectionParams
            Redshift connection parameters
        siteid : str
            site id to query
        column : str
            meas_name to query  (default ac_power)
        sensor : int, optional
            sensor index to query based on number of sensors at the site id (default None)
        tmin : timestamp, optional
            minimum timestamp to query (default None)
        tmax : timestamp, optional
            maximum timestamp to query (default None)
        limit : int, optional
            maximum number of rows to query (default None)
        verbose : bool, optional
            whether to print out timing information (default False)

    Returns
    ------
    df : pd.DataFrame
        Pandas DataFrame containing the queried data
    """

    try:
        import sshtunnel
    except ImportError:
        raise Exception(
            "Please install sshtunnel into your Python environment to use this function"
        )

    try:
        import redshift_connector
    except ImportError:
        raise Exception(
            "Please install redshift_connector into your Python environment to use this function"
        )

    def timing(verbose: bool = True):
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs) -> Any:
                start_time = time()
                result = func(*args, **kwargs)
                end_time = time()
                execution_time = end_time - start_time
                if verbose:
                    print(f"{func.__name__} took {execution_time:.2f} seconds to run")
                return result

            return wrapper

        return decorator

    def create_tunnel_and_connect(
        ssh_params: SSHParams,
    ):
        def decorator(func: Callable):
            @wraps(func)
            def inner_wrapper(
                db_connection_params: DBConnectionParams, *args, **kwargs
            ):
                with sshtunnel.SSHTunnelForwarder(
                    ssh_address_or_host=ssh_params["ssh_address_or_host"],
                    ssh_username=ssh_params["ssh_username"],
                    ssh_pkey=os.path.abspath(ssh_params["ssh_private_key"]),
                    remote_bind_address=ssh_params["remote_bind_address"],
                    host_pkey_directories=[
                        os.path.dirname(os.path.abspath(ssh_params["ssh_private_key"]))
                    ],
                ) as tunnel:
                    if tunnel is None:
                        raise Exception("Tunnel is None")

                    tunnel.start()

                    if tunnel.is_active is False:
                        raise Exception("Tunnel is not active")

                    local_port = tunnel.local_bind_port
                    db_connection_params["port"] = local_port

                    return func(db_connection_params, *args, **kwargs)

            return inner_wrapper

        return decorator

    @timing(verbose)
    @create_tunnel_and_connect(ssh_params)
    def create_df_from_query(
        redshift_params: DBConnectionParams, sql_query: str
    ) -> pd.DataFrame:
        with redshift_connector.connect(**redshift_params) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_query)
                df = cursor.fetch_dataframe()
                return df

    if isinstance(siteid, str) is False:
        raise Exception(f"Siteid must be a string. Siteid is of type {type(siteid)}")

    sensor_not_found: bool = True
    sensor_dict: dict[int, str] = {}
    if sensor is not None:
        if isinstance(sensor, (int, list)) is False:
            raise Exception(
                f"Sensor must be either an int or a list of ints. Sensor is of type {type(sensor)}"
            )

        site_sensor_map_query = f"""
        SELECT sensor FROM measurements
        WHERE site = '{siteid}'
        GROUP BY sensor
        ORDER BY sensor ASC
        """

        site_sensor_df = create_df_from_query(redshift_params, site_sensor_map_query)

        if site_sensor_df is None:
            raise Exception("No data returned from query when getting sensor map")
        sensor_dict = site_sensor_df.to_dict()["sensor"]

        if isinstance(sensor, int):
            sensor_list = [sensor]
        else:
            sensor_list = sensor

        for sensor_index in sensor_list:
            if sensor_index not in sensor_dict:
                raise Exception(
                    f"The index of {sensor_index} for a sensor at site {siteid} is out of bounds. For site {siteid} please choose a sensor index ranging from 0 to {len(sensor_dict) - 1}"
                )
        sensor_not_found = False

    sql_query = f"""
    SELECT site, meas_name, ts, sensor, meas_val_f FROM measurements
    WHERE site = '{siteid}'
    AND meas_name = '{column}'
    """

    # ts_constraint = np.logical_or(tmin is not None, tmax is not None)
    if sensor is not None and not sensor_not_found:
        if isinstance(sensor, list) and len(sensor) > 1:
            sensor_ids = tuple(sensor_dict.get(sensor_index) for sensor_index in sensor)
            sql_query += f"AND sensor IN {sensor_ids}\n"
        else:
            if isinstance(sensor, list):
                sensor = sensor[0]
            sql_query += f"AND sensor = '{sensor_dict.get(sensor)}'\n"
    if tmin is not None:
        if isinstance(tmin, datetime) is False:
            raise Exception(f"tmin must be a datetime. tmin is of type {type(tmin)}")
        sql_query += f"AND ts > '{tmin}'\n"
    if tmax is not None:
        if isinstance(tmax, datetime) is False:
            raise Exception(f"tmax must be a datetime. tmax is of type {type(tmax)}")
        sql_query += f"AND ts < '{tmax}'\n"
    if limit is not None:
        if isinstance(limit, int) is False:
            raise Exception(f"Limit must be an int. Limit is of type {type(limit)}")
        sql_query += f"LIMIT {limit}\n"

    df = create_df_from_query(redshift_params, sql_query)
    if df is None:
        raise Exception("No data returned from query")
    return df


def load_redshift_data_remote(
    siteid: str,
    api_key: str,
    column: str = "ac_power",
    sensor: int | list[int] | None = None,
    tmin: datetime | None = None,
    tmax: datetime | None = None,
    limit: int | None = None,
    verbose: bool = False,
) -> pd.DataFrame:
    """Loads data based on a site id from a Redshift database into a Pandas DataFrame using an SSH tunnel

    Parameters
    ----------
        siteid : str
            site id to query
        api_key : str
            api key for authentication
        column : str
            meas_name to query  (default ac_power)
        sensor : int, optional
            sensor index to query based on number of sensors at the site id (default None)
        tmin : timestamp, optional
            minimum timestamp to query (default None)
        tmax : timestamp, optional
            maximum timestamp to query (default None)
        limit : int, optional
            maximum number of rows to query (default None)
        verbose : bool, optional
            whether to print out timing information (default False)

    Returns
    ------
    df : pd.DataFrame
        Pandas DataFrame containing the queried data
    """

    def timing(verbose: bool = True):
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time()
                result = func(*args, **kwargs)
                end_time = time()
                execution_time = end_time - start_time
                if verbose:
                    print(f"{func.__name__} took {execution_time:.2f} seconds to run")
                return result

            return wrapper

        return decorator

    @timing(verbose)
    def query_redshift_w_api() -> pd.DataFrame:
        url = "https://lmojfukey3rylrbqughzlfu6ca0ujdby.lambda-url.us-west-1.on.aws/"
        payload = {
            "api_key": api_key,
            "siteid": siteid,
            "column": column,
            "sensor": sensor,
            "tmin": str(tmin),
            "tmax": str(tmax),
            "limit": str(limit),
        }

        if sensor is None:
            payload.pop("sensor")
        if tmin is None:
            payload.pop("tmin")
        if tmax is None:
            payload.pop("tmax")
        if limit is None:
            payload.pop("limit")

        response = requests.post(url, json=payload, timeout=60 * 5)
        if response.status_code != 200:
            raise Exception(f"Error {response.status_code} returned from API")
        data = response.json()
        json_data = json.loads(data)

        df = pd.DataFrame(json_data)
        return df

    df = query_redshift_w_api()

    if df is None:
        raise Exception("No data returned from query")
    if df.empty:
        raise Exception("Empty dataframe returned from query")
    return df
