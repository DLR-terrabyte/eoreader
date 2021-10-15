# -*- coding: utf-8 -*-
# Copyright 2021, SERTIT-ICube - France, https://sertit.unistra.fr/
# This file is part of eoreader project
#     https://github.com/sertit/eoreader
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Sentinel-3 products

.. WARNING:
    Not georeferenced NetCDF files are badly opened by GDAL and therefore by rasterio !
    The rasters are flipped (upside/down, we can use `np.flipud`).
    This is fixed by reprojecting with GCPs, BUT is still an issue for metadata files.
    As long as we treat consistent data (geographic rasters???), this should not be problematic
    BUT pay attention to rasters containing several bands (such as solar irradiance for OLCI products)
    -> they are inverted as the columns are ordered reversely
"""
import logging
from abc import abstractmethod
from datetime import datetime
from enum import unique
from pathlib import Path
from typing import Union

import geopandas as gpd
import netCDF4
import numpy as np
import xarray as xr
from cloudpathlib import CloudPath
from lxml import etree
from lxml.builder import E
from rasterio import crs as riocrs
from rasterio.enums import Resampling
from sertit import rasters, vectors
from sertit.misc import ListEnum
from sertit.rasters import XDS_TYPE
from shapely.geometry import Polygon, box

from eoreader import utils
from eoreader.bands.bands import BandNames
from eoreader.bands.bands import OpticalBandNames as obn
from eoreader.exceptions import InvalidProductError
from eoreader.products.optical.optical_product import OpticalProduct
from eoreader.utils import DATETIME_FMT, EOREADER_NAME

LOGGER = logging.getLogger(EOREADER_NAME)


@unique
class S3ProductType(ListEnum):
    """Sentinel-3 products types (not exhaustive, only L1)"""

    OLCI_EFR = "OL_1_EFR___"
    """OLCI EFR Product Type"""

    SLSTR_RBT = "SL_1_RBT___"
    """SLSTR RBT Product Type"""


@unique
class S3Instrument(ListEnum):
    """Sentinel-3 products types"""

    OLCI = "S3_OLCI"
    """OLCI Instrument"""

    SLSTR = "S3_SLSTR"
    """SLSTR Instrument"""


@unique
class S3DataType(ListEnum):
    """Sentinel-3 data types -> only considering useful ones"""

    EFR = "EFR___"
    """EFR Data Type, for OLCI instrument"""

    RBT = "RBT__"
    """RBT Data Type, for SLSTR instrument"""


class S3Product(OpticalProduct):
    """
    Super-Class of Sentinel-3 Products
    """

    def __init__(
        self,
        product_path: Union[str, CloudPath, Path],
        archive_path: Union[str, CloudPath, Path] = None,
        output_path: Union[str, CloudPath, Path] = None,
        remove_tmp: bool = False,
    ) -> None:
        self._data_type = None
        self._gcps = None

        # Geocoding
        self._geo_file = None
        self._lat_nc_name = None
        self._lon_nc_name = None
        self._alt_nc_name = None

        # Tie geocoding
        self._tie_geo_file = None
        self._tie_lat_nc_name = None
        self._tie_lon_nc_name = None

        # Mean Sun angles
        self._geom_file = None
        self._saa_name = None  # Azimuth angle
        self._sza_name = None  # Zenith angle

        # Rad 2 Refl
        self._misc_file = None
        self._solar_flux_name = None

        self._set_preprocess_members()

        super().__init__(
            product_path, archive_path, output_path, remove_tmp
        )  # Order is important here

    @abstractmethod
    def _set_preprocess_members(self):
        """ Set pre-process members """
        raise NotImplementedError("This method should be implemented by a child class")

    def _post_init(self) -> None:
        """
        Function used to post_init the products
        (setting sensor type, band names and so on)
        """
        # Post init done by the super class
        super()._post_init()

    def extent(self) -> gpd.GeoDataFrame:
        """
        Get UTM extent of the tile, managing the case with not orthorectified bands.

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = "S3B_SL_1_RBT____20191115T233722_20191115T234022_20191117T031722_0179_032_144_3420_LN2_O_NT_003.SEN3"
            >>> prod = Reader().open(path)
            >>> prod.utm_extent()
                                                        geometry
            0  POLYGON ((1488846.028 6121896.451, 1488846.028...

        Returns:
            gpd.GeoDataFrame: Footprint in UTM
        """
        # --- EXTENT IN UTM ---
        extent = gpd.GeoDataFrame(
            geometry=[box(*self.footprint().geometry.total_bounds)],
            crs=self.crs(),
        )

        # --- EXTENT IN WGS84 ---
        # # Open lon/lat/alt files
        # lat = rioxarray.open_rasterio(self._get_nc_file_str(self._tie_geo_file, self._tie_lat_nc_name))
        # lon = rioxarray.open_rasterio(self._get_nc_file_str(self._tie_geo_file, self._tie_lon_nc_name))
        #
        # assert lat.data.shape == lon.data.shape
        #
        # # Get min/max of lat/lon
        # def _get_min_max(xds: xr.DataArray) -> tuple:
        #     corners = [xds.data[0, 0, 0], xds.data[0, 0, -1], xds.data[0, -1, 0], xds.data[0, -1, -1]]
        #     return np.min(corners) * xds.scale_factor, np.max(corners) * xds.scale_factor
        #
        # lat_min, lat_max = _get_min_max(lat)
        # lon_min, lon_max = _get_min_max(lon)
        #
        # # Create wgs84 extent (left, bottom, right, top)
        # extent_wgs84 = gpd.GeoDataFrame(
        #     geometry=[
        #         vectors.from_bounds_to_polygon(lon_min, lat_min, lon_max, lat_max)
        #     ],
        #     crs=vectors.WGS84,
        # )

        return extent

    def footprint(self) -> gpd.GeoDataFrame:
        """
        Get UTM footprint in UTM of the products (without nodata, *in french == emprise utile*)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.footprint()
               index                                           geometry
            0      0  POLYGON ((199980.000 4500000.000, 199980.000 4...

        Returns:
            gpd.GeoDataFrame: Footprint as a GeoDataFrame
        """

        # Open lon/lat/alt files
        lat = self._read_nc(self._tie_geo_file, self._tie_lat_nc_name)
        lon = self._read_nc(self._tie_geo_file, self._tie_lon_nc_name)

        assert lat.data.shape == lon.data.shape

        # Get WGS84 vertices
        vertex = [
            (lonv, latv) for lonv, latv in zip(lon.data[0, 0, :], lat.data[0, 0, :])
        ]
        vertex += [
            (lonv, latv) for lonv, latv in zip(lon.data[0, :, -1], lat.data[0, :, -1])
        ]
        vertex += [
            (lonv, latv)
            for lonv, latv in zip(lon.data[0, -1, ::-1], lat.data[0, -1, ::-1])
        ]
        vertex += [
            (lonv, latv)
            for lonv, latv in zip(lon.data[0, ::-1, 0], lat.data[0, ::-1, 0])
        ]

        # Create wgs84 extent (left, bottom, right, top)
        extent_wgs84 = gpd.GeoDataFrame(geometry=[Polygon(vertex)], crs=vectors.WGS84)
        # TODO: set CRS here also (in order not to reopen lat/lon) ?

        return extent_wgs84.to_crs(self.crs())

    def crs(self) -> riocrs.CRS:
        """
        Get UTM projection of the tile

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.crs()
            CRS.from_epsg(32630)

        Returns:
            rasterio.crs.CRS: CRS object
        """
        # Open lon/lat/alt files
        lat = self._read_nc(self._tie_geo_file, self._tie_lat_nc_name)
        lon = self._read_nc(self._tie_geo_file, self._tie_lon_nc_name)

        assert lat.data.shape == lon.data.shape

        # Get lon/lat in the middle of the band
        mid_x = int(lat.x.size / 2)
        mid_y = int(lat.y.size / 2)
        mid_lat = lat[0, mid_y, mid_x].data
        mid_lon = lon[0, mid_y, mid_x].data

        # Deduce UTM proj from the central lon/lat
        utm = vectors.corresponding_utm_projection(mid_lat, mid_lon)

        return riocrs.CRS.from_string(utm)

    def get_datetime(self, as_datetime: bool = False) -> Union[str, datetime]:
        """
        Get the product's acquisition datetime, with format `YYYYMMDDTHHMMSS` <-> `%Y%m%dT%H%M%S`

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = "S3B_SL_1_RBT____20191115T233722_20191115T234022_20191117T031722_0179_032_144_3420_LN2_O_NT_003.SEN3"
            >>> prod = Reader().open(path)
            >>> prod.get_datetime(as_datetime=True)
            datetime.datetime(2019, 11, 15, 23, 37, 22)
            >>> prod.get_datetime(as_datetime=False)
            '20191115T233722'

        Args:
            as_datetime (bool): Return the date as a datetime.datetime. If false, returns a string.

        Returns:
             Union[str, datetime.datetime]: Its acquisition datetime
        """
        # Get MTD XML file
        root, _ = self.read_mtd()

        # Open identifier
        try:
            acq_date = root.findtext(".//start_time")
        except TypeError:
            raise InvalidProductError("start_time not found in metadata !")

        # Convert to datetime
        date = datetime.strptime(acq_date, "%Y-%m-%dT%H:%M:%S.%fZ")

        if not as_datetime:
            date = date.strftime(DATETIME_FMT)

        return date

    @abstractmethod
    def _get_raw_band_path(self, band: Union[obn, str], subdataset: str = None) -> str:
        """
        Return the paths of raw band.

        Args:
            band (Union[obn, str]): Wanted raw bands
            subdataset (str): Subdataset

        Returns:
            str: Raw band path
        """
        raise NotImplementedError("This method should be implemented by a child class")

    def _get_preprocessed_band_path(
        self,
        band: Union[obn, str],
        resolution: Union[float, tuple, list] = None,
        writable=True,
    ) -> Union[CloudPath, Path]:
        """
        Create the pre-processed band path

        Args:
            band (band: Union[obn, str]): Wanted band (quality flags accepted)
            resolution (Union[float, tuple, list]): Resolution of the wanted UTM band
            writable (bool): Do we need to write the pre-processed band ?

        Returns:
            Union[CloudPath, Path]: Pre-processed band path
        """
        res_str = self._resolution_to_str(resolution)
        band_str = band.name if isinstance(band, obn) else band

        return self._get_band_folder(writable=writable).joinpath(
            f"{self.condensed_name}_{band_str}_{res_str}.tif"
        )

    def get_band_paths(self, band_list: list, resolution: float = None) -> dict:
        """
        Return the paths of required bands.

        .. WARNING:: If not existing, this function will orthorectify your bands !

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> from eoreader.bands.alias import *
            >>> path = "S3B_SL_1_RBT____20191115T233722_20191115T234022_20191117T031722_0179_032_144_3420_LN2_O_NT_003.SEN3"
            >>> prod = Reader().open(path)
            >>> prod.get_band_paths([GREEN, RED])
            Executing processing graph
            ...11%...21%...31%...42%...52%...62%...73%...83%... done.
            {
                <OpticalBandNames.GREEN: 'GREEN'>: '20191115T233722_S3_SLSTR_RBT\\S1_reflectance.tif',
                <OpticalBandNames.RED: 'RED'>: '20191115T233722_S3_SLSTR_RBT\\S2_reflectance.tif',
            }

        Args:
            band_list (list): List of the wanted bands
            resolution (float): Useless here

        Returns:
            dict: Dictionary containing the path of each queried band
        """
        band_paths = {}
        for band in band_list:
            # Get clean band path
            clean_band = self._get_clean_band_path(band, resolution=resolution)
            if clean_band.is_file():
                band_paths[band] = clean_band
            else:
                # Pre-process the wanted band (does nothing if existing)
                band_paths[band] = self._preprocess(band, resolution=resolution)

        return band_paths

    # pylint: disable=W0613
    def _read_band(
        self,
        path: Union[CloudPath, Path],
        band: BandNames = None,
        resolution: Union[tuple, list, float] = None,
        size: Union[list, tuple] = None,
    ) -> XDS_TYPE:
        """
        Read band from disk.

        .. WARNING::
            Invalid pixels are not managed here

        Args:
            path (Union[CloudPath, Path]): Band path
            band (BandNames): Band to read
            resolution (Union[tuple, list, float]): Resolution of the wanted band, in dataset resolution unit (X, Y)
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
        Returns:
            XDS_TYPE: Band xarray

        """
        band = utils.read(
            path, resolution=resolution, size=size, resampling=Resampling.bilinear
        )

        # Read band
        return band.astype(np.float32) * band.scale_factor

    @abstractmethod
    def _manage_invalid_pixels(self, band_arr: XDS_TYPE, band: obn) -> XDS_TYPE:
        """
        Manage invalid pixels (Nodata, saturated, defective...)

        Args:
            band_arr (XDS_TYPE): Band array
            band (obn): Band name as an OpticalBandNames

        Returns:
            XDS_TYPE: Cleaned band array
        """
        raise NotImplementedError("This method should be implemented by a child class")

    def _load_bands(
        self, bands: list, resolution: float = None, size: Union[list, tuple] = None
    ) -> dict:
        """
        Load bands as numpy arrays with the same resolution (and same metadata).

        Args:
            bands (list): List of the wanted bands
            resolution (float): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        # Return empty if no band are specified
        if not bands:
            return {}

        # Get band paths
        if not isinstance(bands, list):
            bands = [bands]

        if resolution is None and size is not None:
            resolution = self._resolution_from_size(size)
        band_paths = self.get_band_paths(bands, resolution=resolution)

        # Open bands and get array (resampled if needed)
        band_arrays = self._open_bands(band_paths, resolution=resolution, size=size)

        return band_arrays

    @abstractmethod
    def _preprocess(
        self,
        band: Union[obn, str],
        resolution: float = None,
        to_reflectance: bool = True,
        subdataset: str = None,
    ) -> Union[CloudPath, Path]:
        """
        Pre-process S3 bands:
        - Geocode
        - Convert radiance to reflectance

        Args:
            band (Union[obn, str]): Band to preprocess (quality flags or others are accepted)
            resolution (float): Resolution
            to_reflectance (bool): Convert band to reflectance
            subdataset (str): Subdataset

        Returns:
            dict: Dictionary containing {band: path}
        """
        raise NotImplementedError("This method should be implemented by a child class")

    def _get_condensed_name(self) -> str:
        """
        Get S3 products condensed name ({date}_S3_{tile]_{product_type}).

        Returns:
            str: Condensed name
        """
        return f"{self.get_datetime()}_{self.platform.name}_{self._data_type.name}"

    def get_mean_sun_angles(self) -> (float, float):
        """
        Get Mean Sun angles (Azimuth and Zenith angles)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = "S3B_SL_1_RBT____20191115T233722_20191115T234022_20191117T031722_0179_032_144_3420_LN2_O_NT_003.SEN3"
            >>> prod = Reader().open(path)
            >>> prod.get_mean_sun_angles()
            (78.55043955912154, 31.172127033319388)

        Returns:
            (float, float): Mean Azimuth and Zenith angle
        """
        # Open sun azimuth and zenith files
        sun_az = self._read_nc(self._geom_file, self._saa_name)
        sun_ze = self._read_nc(self._geom_file, self._sza_name)

        return sun_az.mean().data % 360, sun_ze.mean().data

    def _read_mtd(self) -> (etree._Element, dict):
        """
        Read metadata and outputs the metadata XML root and its namespaces as a dict

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = "S3B_SL_1_RBT____20191115T233722_20191115T234022_20191117T031722_0179_032_144_3420_LN2_O_NT_003.SEN3"
            >>> prod = Reader().open(path)
            >>> prod.read_mtd()
            (<Element level1Product at 0x1b845b7ab88>, '')

        Returns:
            (etree._Element, dict): Metadata XML root and its namespace
        """
        # Open first nc file as every file should have the global attributes
        # Here in read_mtd we don't know which type of product we have (before we have the correct platform)
        geom_file = self.path.joinpath(self._tie_geo_file)
        if not geom_file.is_file():
            raise InvalidProductError("This Sentinel-3 product has no geometry file !")

        # Open DS
        if isinstance(geom_file, CloudPath):
            netcdf_ds = netCDF4.Dataset(
                geom_file.download_to(self._get_band_folder(writable=True))
            )
        else:
            netcdf_ds = netCDF4.Dataset(geom_file)

        # Parsing global attributes
        global_attr_names = [
            "absolute_orbit_number",
            "comment",
            "contact",
            "creation_time",
            "history",
            "institution",
            "netCDF_version",
            "product_name",
            "references",
            "resolution",
            "source",
            "start_offset",
            "start_time",
            "stop_time",
            "title",
            # OLCI
            "ac_subsampling_factor",
            "al_subsampling_factor",
            # SLSTR
            "track_offset",
        ]

        global_attr = [
            E(attr, str(getattr(netcdf_ds, attr)))
            for attr in global_attr_names
            if hasattr(netcdf_ds, attr)
        ]

        mtd = E.s3_global_attributes(*global_attr)
        mtd_el = etree.fromstring(
            etree.tostring(
                mtd, pretty_print=True, xml_declaration=True, encoding="UTF-8"
            )
        )

        return mtd_el, {}

    def _get_nc_path_str(self, filename: str, subdataset: str = None) -> str:
        """
        Get NetCDF file path.

        NetCDF paths are supposed to be at the root of this product.

        Returns a string as it is meant to be opened by rasterio.

        Caches the file if needed (rasterio does not seem to be able to open a netcdf stored in the cloud).

        Args:
            filename (str): Filename
            subdataset (str): NetCDF subdataset if needed

        Returns:
            str: NetCDF file path as a string
        """
        if isinstance(self.path, CloudPath):
            path = self.path.joinpath(filename).download_to(
                self._get_band_folder(writable=True)
            )
        else:
            path = str(self.path.joinpath(filename))

        # Complete the path
        path = f"netcdf:{path}"

        if subdataset:
            path += f":{subdataset}"

        return path

    def _read_nc(self, filename: str, subdataset: str = None) -> xr.DataArray:
        """
        Read NetCDF file (as float32) and rescaled them to their true values

        NetCDF files are supposed to be at the root of this product.

        Args:
            filename (str): Filename
            subdataset (str): NetCDF subdataset if needed

        Returns:
            xr.DataArray: NetCDF file, rescaled
        """
        # Open with rioxarray directly as these files are not geocoded
        nc = rasters.read(self._get_nc_path_str(filename, subdataset))

        # NO NEED TO FLIP IF WE STAY VIGILANT
        # nc = nc.copy(data=np.flipud(nc)).astype(np.float32)
        # return nc * nc.scale_factor

        return nc.astype(np.float32) * nc.scale_factor

    @abstractmethod
    def _load_clouds(
        self, bands: list, resolution: float = None, size: Union[list, tuple] = None
    ) -> dict:
        """
        Load cloud files as xarrays.

        Args:
            bands (list): List of the wanted bands
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        raise NotImplementedError("This method should be implemented by a child class")

    @abstractmethod
    def _set_resolution(self) -> float:
        """
        Set product default resolution (in meters)
        """
        raise NotImplementedError("This method should be implemented by a child class")

    @abstractmethod
    def _set_product_type(self) -> None:
        """
        Set product type
        """
        raise NotImplementedError("This method should be implemented by a child class")
