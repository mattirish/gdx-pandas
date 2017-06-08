from __future__ import absolute_import, print_function
from builtins import super

from collections import defaultdict, OrderedDict
from collections.abc import MutableSequence
from enum import Enum
import logging
from six import string_types

# try to import gdx loading utility
HAVE_GDX2PY = False
try:
    import gdx2py
    HAVE_GDX2PY = True
except ImportError: pass

import gdxcc
import pandas as pds

from gdxpds import Error
from gdxpds.tools import NeedsGamsDir

logger = logging.getLogger(__name__)


class GdxError(Error):
    def __init__(self, H, msg):
        """
        Pulls information from gdxcc about the last encountered error and appends
        it to msg.

        Positional Arguments:
            - H (pointer or None) - SWIG binding pointer to a GDX object
            - msg (str) - gdxpds error message
        """
        self.msg = msg + "."
        if H:
            self.msg += " " + gdxcc.gdxErrorStr(H, gdxcc.gdxGetLastError(H))[1] + "."
        super().__init__(self.msg)


class GdxFile(MutableSequence, NeedsGamsDir):

    def __init__(self,gams_dir=None,lazy_load=True):
        """
        Initializes a GdxFile object by connecting to GAMS and creating a pointer.

        Throws a GdxError if either of those operations fail.
        """
        self.lazy_load = lazy_load
        self._version = None
        self._producer = None
        self._filename = None
        # HERE -- Replace with a Universal Set
        self.universal_set = None
        self._symbols = OrderedDict()

        NeedsGamsDir.__init__(self,gams_dir=gams_dir)
        self._H = self._create_gdx_object()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        gdxcc.gdxFree(self.H)

    @property
    def empty(self):
        """
        Returns True if this GdxFile object contains any symbols.
        """
        return len(self) == 0

    @property
    def H(self):
        """
        GDX object handle
        """
        return self._H

    @property
    def filename(self):
        return self._filename

    @property
    def version(self):
        """
        GDX file version
        """
        return self._version

    @property
    def producer(self):
        """
        What program wrote the GDX file
        """
        return self._producer

    @property
    def num_elements(self):
        return sum([symbol.num_records for symbol in self])

    def read(self,filename):
        """
        Opens gdx file at filename and reads meta-data. If not self.lazy_load, 
        also loads all symbols.

        Throws an Error if not self.empty.

        Throws a GdxError if any calls to gdxcc fail.
        """
        if not self.empty:
            raise Error("GdxFile.read can only be used if the GdxFile is .empty")

        # open the file
        rc = gdxcc.gdxOpenRead(self.H,filename)
        if not rc[0]:
            raise GdxError(self.H,"Could not open '{}'".format(filename))
        self._filename = filename

        # read in meta-data ...
        # ... for the file
        ret, self._version, self._producer = gdxcc.gdxFileVersion(self.H)
        if ret != 1: 
            raise GDXError(self.H,"Could not get file version")
        ret, symbol_count, element_count = gdxcc.gdxSystemInfo(self.H)
        logger.info("Opening '{}' with {} symbols and {} elements with lazy_load = {}.".format(filename,symbol_count,element_count,self.lazy_load))
        # ... for the symbols
        ret, name, dims, data_type = gdxcc.gdxSymbolInfo(self.H,0)
        if ret != 1:
            raise GdxError(self.H,"Could not get symbol info for the universal set")
        self.universal_set = GdxSymbol(name,data_type,dims=dims,file=self,index=0)
        for i in range(symbol_count):
            index = i + 1
            ret, name, dims, data_type = gdxcc.gdxSymbolInfo(self.H,index)
            if ret != 1:
                raise GdxError(self.H,"Could not get symbol info for symbol {}".format(index))
            self.append(GdxSymbol(name,data_type,dims=dims,file=self,index=index))

        # read all symbols if not lazy_load
        if not self.lazy_load:
            for symbol in self:
                symbol.load()
        return

    def write(self,filename):
        # only write if all symbols loaded
        for symbol in self:
            if not symbol.loaded:
                raise Error("All symbols must be loaded before this file can be written.")

        ret = gdxcc.gdxOpenWrite(self.H,filename,"gdxpds")
        if not ret:
            raise GdxError(self.H,"Could not open {} for writing.".format(repr(filename)))
        self._filename = filename
        
        # write the universal set
        self.universal_set.write()

        for symbol in self:
            symbol.write()

        gdxcc.gdxClose(self.H)

    def __repr__(self):
        return "GdxFile(self,gams_dir={},lazy_laod={})".format(
                   repr(self.gams_dir),
                   repr(self.lazy_load))

    def __str__(self):
        s = "GdxFile containing {} symbols and {} elements.".format(len(self),self.num_elements)
        sep =  " Symbols:\n  "
        for symbol in self:
            s += sep + str(symbol)
            sep = "\n  "
        return s

    def __getitem__(self,key):
        """
        Supports list-like indexing and symbol-based indexing
        """
        return self._symbols[self._name_key(key)]

    def __setitem__(self,key,value):
        self._check_insert_setitem(key, value)
        value._file = self
        if key < len(self):
            self._symbols[self._name_key(key)] = value
            self._fixup_name_keys()
            return
        assert key == len(self)
        self._symbols[value.name] = value
        return

    def __delitem__(self,key):
        del self._symbols[self._name_key(key)]
        return

    def __len__(self):
        return len(self._symbols)

    def insert(self,key,value):
        self._check_insert_setitem(key, value)
        value._file = self
        data = [(symbol.name, symbol) for symbol in self]
        data.insert(key,(value.name,value))
        self._symbols = OrderedDict(data)
        return

    def _name_key(self,key):
        name_key = key
        if isinstance(key,int):
            name_key = list(self._symbols.keys())[key]
        return name_key

    def _check_insert_setitem(self,key,value):
        if not isinstance(value,GdxSymbol):
            raise Error("GdxFiles only contain GdxSymbols. GdxFile was given a {}.".format(type(value)))
        if not isinstance(key,int):
            raise Error("When adding or replacing GdxSymbols in GdxFiles, only integer, not name indices, may be used.")
        if key > len(self):
            raise Error("Invalid key, {}".format(key))
        return

    def _fixup_name_keys(self):
        self._symbols = OrderedDict([(symbol.name, symbol) for cur_key, symbol in self._symbols])
        return        

    def _create_gdx_object(self):
        H = gdxcc.new_gdxHandle_tp()
        rc = gdxcc.gdxCreateD(H,self.gams_dir,gdxcc.GMS_SSSIZE)
        if not rc[0]:
            raise GdxError(H,rc[1])
        return H


class GamsDataType(Enum):
    Set = gdxcc.GMS_DT_SET
    Parameter = gdxcc.GMS_DT_PAR
    Variable = gdxcc.GMS_DT_VAR
    Equation = gdxcc.GMS_DT_EQU
    Alias = gdxcc.GMS_DT_ALIAS


class GamsVariableType(Enum):
    Unknown = gdxcc.GMS_VARTYPE_UNKNOWN
    Binary = gdxcc.GMS_VARTYPE_BINARY
    Integer = gdxcc.GMS_VARTYPE_INTEGER
    Positive = gdxcc.GMS_VARTYPE_POSITIVE
    Negative = gdxcc.GMS_VARTYPE_NEGATIVE
    Free = gdxcc.GMS_VARTYPE_FREE
    SOS1 = gdxcc.GMS_VARTYPE_SOS1
    SOS2 = gdxcc.GMS_VARTYPE_SOS2
    Semicont = gdxcc.GMS_VARTYPE_SEMICONT
    Semiint = gdxcc.GMS_VARTYPE_SEMIINT


class GamsValueType(Enum):
    Level = gdxcc.GMS_VAL_LEVEL       # .l
    Marginal = gdxcc.GMS_VAL_MARGINAL # .m
    Lower = gdxcc.GMS_VAL_LOWER       # .lo
    Upper = gdxcc.GMS_VAL_UPPER       # .ub
    Scale = gdxcc.GMS_VAL_SCALE       # .scale


GAMS_VALUE_COLS_MAP = defaultdict(lambda : [('Value',GamsValueType.Level.value)])
GAMS_VALUE_COLS_MAP[GamsDataType.Variable] = [(value_type.name, value_type.value) for value_type in GamsValueType]
GAMS_VALUE_COLS_MAP[GamsDataType.Equation] = GAMS_VALUE_COLS_MAP[GamsDataType.Variable]


class GdxSymbol(object): 
    def __init__(self,name,data_type,dims=0,file=None,index=None,
                 description='',variable_type=None): 
        self._name = name
        self.description = description
        self._loaded = False
        self._data_type = GamsDataType(data_type)
        self._variable_type = None; self.variable_type = variable_type
        self._dataframe = None
        self.dims = dims       
        assert self._dataframe is not None
        self._file = file
        self._index = index        

        if self.file:
            # reading from file
            # get additional meta-data
            ret, records, userinfo, description = gdxcc.gdxSymbolInfoX(self.file.H,self.index)
            if ret != 1:
                raise GdxError(self.file.H,"Unable to get extended symbol information for {}".format(self.name))
            self._num_records = records
            if self.data_type == GamsDataType.Variable:
                self.variable_type = GamsVariableType(userinfo)
            self.description = description
            if self.index > 0:
                ret, gdx_domain = gdxcc.gdxSymbolGetDomainX(self.file.H,self.index)
                if ret == 0:
                    raise GdxError(self.file.H,"Unable to get domain information for {}".format(self.name))
                assert len(gdx_domain) == len(self.dims), "Dimensional information read in from GDX should be consistent."
                self.dims = gdx_domain
            return
        
        # writing new symbol
        self._loaded = True

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self,value):
        self._name = value
        if self.file:
            self.file._fixup_name_keys()
        return

    @property
    def data_type(self):
        return self._data_type

    @data_type.setter
    def data_type(self, value):
        if not self.loaded or self.num_records > 0:
            raise Error("Cannot change the data_type of a GdxSymbol that is yet to be read for file or contains records.")
        self._data_type = GamsDataType(value)
        self.variable_type = None
        self._init_dataframe()
        return

    @property
    def variable_type(self):
        return self._variable_type

    @variable_type.setter
    def variable_type(self,value):
        if self.data_type == GamsDataType.Variable:
            try:
                self._variable_type = GamsVariableType(value)
            except:
                if isinstance(self._variable_type,GamsVariableType):
                    logger.debug("Ignoring invalid GamsVariableType request.")
                    return
                logger.debug("Setting variable_type to {}.".format(GamsVariableType.Free))
                self._variable_type = GamsVariableType.Free
            return
        assert self.data_type != GamsDataType.Variable
        if value is not None:
            logger.warn("GdxSymbol is not a Variable, so setting variable_type to None")
        self._variable_type = None

    @property
    def value_cols(self):
        return GAMS_VALUE_COLS_MAP[self.data_type]

    @property
    def value_col_names(self):
        return [col_name for col_name, col_ind in self.value_cols]            

    @property
    def file(self):
        return self._file

    @property
    def index(self):
        return self._index

    @property
    def loaded(self):
        return self._loaded

    @property
    def full_typename(self):
        if self.data_type == GamsDataType.Parameter and self.dims == 0:
            return 'Scalar'
        elif self.data_type == GamsDataType.Variable:
            return self.variable_type.name + " " + self.data_type.name
        return self.data_type.name

    @property
    def dims(self):
        return self._dims

    @dims.setter
    def dims(self, value):
        if self.loaded and self.num_records > 0:
            if not isinstance(value,list) or len(value) != self.num_dims:
                logger.warn("Cannot set dims to {}, because dataframe with dims {} already contains data.".format(value,self.dims))
        if isinstance(value,int):
            self._dims = ['*'] * value
            self._init_dataframe()
            return
        if not isinstance(value, list):
            raise Error('dims must be an int or a list. Was passed {} of type {}.'.format(value, type(value)))
        for dim in value:
            if not isinstance(dim,string_types):
                raise Error('Individual dimensions must be denoted by strings. Was passed {} as element of {}.'.format(dim, value))
        if self.num_dims > 0 and self.num_dims != len(value):
            logger.warn("{}'s number of dimensions is changing from {} to {}.".format(self.name,self.num_dims,len(value)))
        self._dims = value
        if self.loaded and self.num_records > 0:
            self._dataframe.columns = self.dims + self.value_col_names
            return
        self._init_dataframe()

    @property
    def num_dims(self):
        return len(self.dims)        

    @property
    def dataframe(self):
        return self._dataframe

    @dataframe.setter
    def dataframe(self, data):
        if isinstance(data, pds.DataFrame):
            # Fix up dimensions
            num_dims = len(data.columns) - len(self.value_cols)
            dim_cols = data.columns[:num_dims]
            replace_dims = True
            for col in dim_cols:
                if not isinstance(col,string_types):
                    replace_dims = False
                    break
            if replace_dims:
                self.dims = dim_cols
            if num_dims != self.num_dims:
                self.dims = num_dims
            self._dataframe = copy.deepcopy(data)
            self._dataframe.columns = self.dims + self.value_col_names
        else:
            self._dataframe = pds.DataFrame(data,columns=self.dims + self.value_col_names)
        return

    def _init_dataframe(self):
        self._dataframe = pds.DataFrame([],columns=self.dims + self.value_col_names)
        return

    @property
    def num_records(self):
        if self.loaded:
            return len(self.dataframe.index)
        return self._num_records

    def __repr__(self):
        return "GdxSymbol({},{},{},file={},index={},description={},variable_type={})".format(
                   repr(self.name),
                   repr(self.data_type),
                   repr(self.dims),
                   repr(self.file),
                   repr(self.index),
                   repr(self.description),
                   repr(self.variable_type))

    def __str__(self):
        s = self.name
        s += ", " + self.description    
        s += ", " + self.full_typename    
        s += ", {} records".format(self.num_records)
        s += ", {} dims {}".format(self.num_dims, self.dims)
        s += ", loaded" if self.loaded else ", not loaded"
        return s

    def load(self):
        if self.loaded:
            logger.info("Nothing to do. Symbol already loaded.")
            return
        if not self.file:
            raise Error("Cannot load {} because there is no file pointer".format(repr(self)))
        if not self.index:
            raise Error("Cannot load {} because there is no symbol index".format(repr(self)))

        if self.data_type == GamsDataType.Parameter and HAVE_GDX2PY:
            self.dataframe = gdx2py.par2list(self.file.filename,self.name) 
            self._loaded = True
            return

        data = []
        ret, records = gdxcc.gdxDataReadStrStart(self.file.H,self.index)
        for i in range(records):
            ret, elements, values, afdim = gdxcc.gdxDataReadStr(self.file.H)
            data.append(elements + [values[col_ind] for col_name, col_ind in self.value_cols])
            if self.data_type == GamsDataType.Set:
                data[-1][-1] = True
                # gdxdict called gdxGetElemText here, but I do not currently see value in doing that
        self.dataframe = data
        self._loaded = True
        return

    def write(self):
        # HERE

