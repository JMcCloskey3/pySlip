# -*- coding: utf-8 -*-

"""
A 'slippy map' widget for wxPython.

So why is this widget called 'pySlip'?

Well, in the OpenStreetMap world[1], a 'slippy map' is a browser map view
served by a tile server that can be panned and zoomed in the same way as
popularised by Google maps.  Such a map feels 'slippery', I guess.

Rather than 'slippy' I went for the slightly more formal 'pySlip' since the
thing is written in Python and therefore must have the obligatory 'py' prefix.

Even though this was originally written for a geographical application, the
*underlying* system only assumes a cartesian 2D coordinate system.  The tile
source must translate between the underlying coordinates and whatever coordinate
system the tiles use.  So pySlip could be used to present a game map, 2D CAD
view, etc, as well as Mercator tiles provided either locally from the filesystem
or from the internet (OpenStreetMap, for example).

[1] http://wiki.openstreetmap.org/index.php/Slippy_Map
"""


import os
import sys
import glob
import json
try:
    import cPickle as pickle
except ImportError:
    import pickle
import traceback
import wx

# if we don't have log.py, don't crash
try:
    import pyslip.log as log
    log = log.Log('pyslip.log', log.Log.DEBUG)
except ImportError:
    def log(*args, **kwargs):
        pass


# type of SELECT events
(EventLevel, EventPosition, EventSelect, EventBoxSelect,
    EventPolySelect, EventPolyBoxSelect, EventRightSelect) = range(7)


######
# Base class for the widget canvas - buffered and flicker-free.
######

class _BufferedCanvas(wx.Panel):
    """Implements a buffered, flicker-free canvas widget.

    This class is based on:
        http://wiki.wxpython.org/index.cgi/BufferedCanvas
    """

    # The backing buffer
    buffer = None

    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.NO_FULL_REPAINT_ON_RESIZE):
        """Initialise the canvas.

        parent  reference to 'parent' widget
        id      the unique widget ID (NB: shadows builtin 'id')
        pos     canvas position
        size    canvas size
        style   wxPython style
        """

        wx.Panel.__init__(self, parent, id, pos, size, style)

        # Bind events
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_SIZE, self.OnSize)

        # Disable background erasing (flicker-licious)
        def disable_event(*args, **kwargs):
            pass            # the sauce, please
        self.Bind(wx.EVT_ERASE_BACKGROUND, disable_event)

        # set callback upon onSize event
        self.onSizeCallback = None

    def Draw(self, dc):
        """Stub: called when the canvas needs to be re-drawn."""

        pass

    def Update(self):
        """Causes the canvas to be updated."""

        dc = wx.BufferedDC(wx.ClientDC(self), self.buffer)
        dc.BeginDrawing()
        dc.Clear()      # because maybe view size > map size
        self.Draw(dc)
        dc.EndDrawing()

    def OnPaint(self, event):
        """Paint the canvas to the screen."""

        # Blit the front buffer to the screen
        wx.BufferedPaintDC(self, self.buffer)

    def OnSize(self, event=None):
        """Create a new off-screen buffer to hold drawn data."""

        (width, height) = self.GetClientSizeTuple()
        if width == 0:
            width = 1
        if height == 0:
            height = 1
        self.view_width = width
        self.view_height = height

        # new off-screen buffer
        self.buffer = wx.EmptyBitmap(width, height)

        # call onSize callback, if registered
        if self.onSizeCallback:
            self.onSizeCallback()

        # Now update the screen
        self.Update()

######
# A layer class - encapsulates all layer data.
######

class _Layer(object):
    """A Layer object."""

    DefaultDelta = 50      # default selection delta

    def __init__(self, id=0, painter=None, data=None, map_rel=True,
                 visible=False, show_levels=None, selectable=False,
                 name="<no name given>", type=None):
        """Initialise the Layer object.

        id           unique layer ID
        painter      render function
        data         the layer data
        map_rel      True if layer is map-relative, else layer-relative
        visible      layer visibility
        show_levels  list of levels at which to auto-show the level
        selectable   True if select operates on this layer, else False
        name         the name of the layer (for debug)
        type         a layer 'type' flag
        """

        self.painter = painter          # routine to draw layer
        self.data = data                # data that defines the layer
        self.map_rel = map_rel          # True if layer is map relative
        self.visible = visible          # True if layer visible
        self.show_levels = show_levels  # None or list of levels to auto-show
        self.selectable = selectable    # True if we can select on this layer
        self.delta = self.DefaultDelta  # minimum distance for selection
        self.name = name                # name of this layer
        self.type = type                # type of layer
        self.id = id                    # ID of this layer

    def __str__(self):
        return ('<pyslip Layer: id=%d, name=%s, map_rel=%s, visible=%s'
                % (self.id, self.name, str(self.map_rel), str(self.visible)))

###############################################################################
# A Resource class that abstracts loading/storing resources from/to disk.
###############################################################################

class Resource(object):
    """A class to allow the loading of layer data to/from disk as a resource.

    An instance of Resource has the following attributes/methods:
        .layers      a dictionary of named Layer objects
        .AddLayer()  add a layer to the resource
        .GetLayer()  get a layer resource by name and type
        .Read()      read a resource from disk
        .Write()     write resource to disk
    """

    def __init__(self, fname=None):
        """Initialise a Resource object, optionally loading data from disk.

        fname  path to a resource file to read
        """

        # set default attributes
        self.layers = {}
        self.filename = fname
        if fname:
            self.Read(fname)

    def Read(self, fname):
        """Read a resource from disk.

        fname  path to file to read

        fname overwrites self.filename.
        """

        self.filename = fname

        try:
            with open(fname) as fp:
                self.layers = json.load(fp)
        except IOError, e:
            msg = 'Error opening %s: %s' % (fname, str(e))
            raise IOError(msg)

    def Write(self, fname=None):
        """Write the Resource to disk.

        fname  path to file to write (default is load self.filename)

        If fname is supplied, it overwrites self.filename.
        """

        if fname:
            self.filename = fname

        if not self.filename:
            raise Exception('Write() called but no filename supplied')

        with open(self.filename, 'wb') as fp:
            json.dump(self.layers, fp, ensure_ascii=False,
                      indent=2, separators=(',', ':'))

    def AddLayer(self, name, layer_type, data):
        """Add a layer to the Resource.

        name        name of the layer
        layer_type  type of the layer
        data        layer data
        """

        self.layers[name] = (layer_type, data)

    def GetLayers(self):
        """Get layers object.

        Returns a dict: {'layer_name': <layer_data>, ...}
        """

        return self.layers

    def GetLayer(self, name):
        """Get a layer by name.

        name  name of the layer to get

        Returns a tuple (layer_type, data), or None if not found.
        """

        return self.layers.get(name, None)

    def DeleteLayer(self, name):
        """Delete a layer by name.

        name  name of the layer to delete
        """

        try:
            del self.layers[name]
        except KeyError:
            pass

    def __len__(self):
        """Makes len(Resource) return number of layers held."""

        return len(self.layers)

###############################################################################
# The wxPython pySlip widget events.
# define the events that are raised by the pySlip widget
###############################################################################

# level change
_myEVT_PYSLIP_LEVEL = wx.NewEventType()
EVT_PYSLIP_LEVEL = wx.PyEventBinder(_myEVT_PYSLIP_LEVEL, 1)

# mouse geo position change
_myEVT_PYSLIP_POSITION = wx.NewEventType()
EVT_PYSLIP_POSITION = wx.PyEventBinder(_myEVT_PYSLIP_POSITION, 1)

# point select
_myEVT_PYSLIP_SELECT = wx.NewEventType()
EVT_PYSLIP_SELECT = wx.PyEventBinder(_myEVT_PYSLIP_SELECT, 1)

# point box-select
_myEVT_PYSLIP_BOXSELECT = wx.NewEventType()
EVT_PYSLIP_BOXSELECT = wx.PyEventBinder(_myEVT_PYSLIP_BOXSELECT, 1)

# polygon select
_myEVT_PYSLIP_POLYSELECT = wx.NewEventType()
EVT_PYSLIP_POLYSELECT = wx.PyEventBinder(_myEVT_PYSLIP_POLYSELECT, 1)

# polygon box-select
_myEVT_PYSLIP_POLYBOXSELECT = wx.NewEventType()
EVT_PYSLIP_POLYBOXSELECT = wx.PyEventBinder(_myEVT_PYSLIP_POLYBOXSELECT, 1)

# point RIGHT select
_myEVT_PYSLIP_RIGHTSELECT = wx.NewEventType()
EVT_PYSLIP_RIGHTSELECT = wx.PyEventBinder(_myEVT_PYSLIP_RIGHTSELECT, 1)


class _PySlipEvent(wx.PyCommandEvent):
    """Event sent from the pySlip widget."""

    def __init__(self, eventType, id):
        """Construct a PySlip event.

        eventType  type of event
        id         unique event number

        Event will be adorned with attributes by raising code.
        """

        wx.PyCommandEvent.__init__(self, eventType, id)

###############################################################################
# The wxPython pySlip widget proper
###############################################################################

class PySlip(_BufferedCanvas):
    """A widget to display a tiled map, à la Google maps."""

    # list of valid placement values
    valid_placements = ['cc', 'nw', 'cn', 'ne', 'ce',
                        'se', 'cs', 'sw', 'cw', None, False, '']

    # panel background colour
    BackgroundColour = '#808080'

    # default point attributes - map relative
    DefaultPointPlacement = 'cc'
    DefaultPointRadius = 3
    DefaultPointColour = wx.RED
    DefaultPointOffsetX = 0
    DefaultPointOffsetY = 0
    DefaultPointData = None

    # default point attributes - view relative
    DefaultPointViewPlacement = 'cc'
    DefaultPointViewRadius = 3
    DefaultPointViewColour = wx.RED
    DefaultPointViewOffsetX = 0
    DefaultPointViewOffsetY = 0
    DefaultPointViewData = None

    # default image attributes - map relative
    DefaultImagePlacement = 'nw'
    DefaultImageRadius = 0
    DefaultImageColour = wx.BLACK
    DefaultImageOffsetX = 0
    DefaultImageOffsetY = 0
    DefaultImageData = None

    # default image attributes - view relative
    DefaultImageViewPlacement = 'nw'
    DefaultImageViewRadius = 0
    DefaultImageViewColour = wx.BLACK
    DefaultImageViewOffsetX = 0
    DefaultImageViewOffsetY = 0
    DefaultImageViewData = None

    # default text attributes - map relative
    DefaultTextPlacement = 'nw'
    DefaultTextRadius = 2
    DefaultTextColour = wx.BLACK
    DefaultTextTextColour = wx.BLACK
    DefaultTextOffsetX = 5
    DefaultTextOffsetY = 1
    DefaultTextFontname = 'Arial'
    DefaultTextFontSize = 10
    DefaultTextData = None

    # default text attributes - view relative
    DefaultTextViewPlacement = 'nw'
    DefaultTextViewRadius = 0
    DefaultTextViewColour = wx.BLACK
    DefaultTextViewTextColour = wx.BLACK
    DefaultTextViewOffsetX = 0
    DefaultTextViewOffsetY = 0
    DefaultTextViewFontname = 'Arial'
    DefaultTextViewFontSize = 10
    DefaultTextViewData = None

    # default polygon attributes - map view
    DefaultPolyPlacement = 'cc'
    DefaultPolyWidth = 1
    DefaultPolyColour = wx.RED
    DefaultPolyClose = False
    DefaultPolyFilled = False
    DefaultPolyFillcolour = 'blue'
    DefaultPolyOffsetX = 0
    DefaultPolyOffsetY = 0
    DefaultPolyData = None

    # default polygon attributes - view relative
    DefaultPolyViewPlacement = 'nw'
    DefaultPolyViewWidth = 1
    DefaultPolyViewColour = wx.RED
    DefaultPolyViewClose = False
    DefaultPolyViewFilled = False
    DefaultPolyViewFillcolour = 'blue'
    DefaultPolyViewOffsetX = 0
    DefaultPolyViewOffsetY = 0
    DefaultPolyViewData = None

    # layer type values
    (TypePoint, TypeImage, TypeText, TypePoly) = range(4)


    def __init__(self, parent, tile_src=None, start_level=None,
                 min_level=None, max_level=None, tilesets=None, **kwargs):
        """Initialise a pySlip instance.

        parent       reference to parent object
        tile_src     the Tiles source object
        start_level  initial tile level to start at
        min_level    the minimum tile level to use
        max_level    the maximum tile level to use
        tilesets     optional list of user tileset directories
        **kwargs     keyword args for Panel
        """

        # create and initialise the base panel
        _BufferedCanvas.__init__(self, parent=parent, **kwargs)
        self.SetBackgroundColour(PySlip.BackgroundColour)

        # save tile source object
        self.tiles = tile_src

        # append user tileset directories to sys.path
        if tilesets:
            for ts_path in tilesets:
                ts_path_abs = os.path.abspath(ts_path)
                sys.path.append(ts_path_abs)

        # set tile levels stuff - allowed levels, etc
        self.max_level = max_level if max_level else self.tiles.max_level
        self.min_level = min_level if min_level else self.tiles.min_level
        self.level = start_level if start_level else self.min_level

        self.tile_size_x = self.tiles.tile_size_x
        self.tile_size_y = self.tiles.tile_size_y

    ######
    # set some internal state
    ######

        # view size in pixels, set properly in OnSize()
        self.view_width = None
        self.view_height = None

        # map size in pixels
        self.map_width = None       # set in UseLevel()
        self.map_height = None

        self.view_offset_x = 0          # map pixel offset at left & top of view
        self.view_offset_y = 0

        # maximum X and Y offset of view (set in ResizeCallback())
        self.max_x_offset = None
        self.max_y_offset = None

        # view left+right lon and top+bottom lat (set in OnSize())
        self.view_llon = self.view_rlon = None
        self.view_tlat = self.view_blat = None

        # various other state variables
        self.was_dragging = False               # True if dragging map
        self.last_drag_x = None                 # previous drag position
        self.last_drag_y = None

        self.ignore_next_up = False             # ignore next LEFT UP event
        self.ignore_next_right_up = False       # ignore next RIGHT UP event

        self.is_box_select = False              # True if box selection
        self.sbox_1_x = self.sbox_1_y = None    # box size

        # layer stuff (no layers at this point)
        self.next_layer_id = 1      # source of unique layer IDs
        self.layer_z_order = []     # layer Z order, contains layer IDs
        self.layer_mapping = {}     # maps layer ID to layer data

        # True if we send event to report mouse position in view
        self.mouse_position_event = True

        # True if event on right mouse click (right button up event)
        self.right_click_event = False

        # True if we send event on level change
        self.change_level_event = True

        # default cursor
        self.default_cursor = wx.CURSOR_DEFAULT

        # cache values
#        self.last_setfont = None        # last font and size (font, size)

        # state of the SHIFT key
        self.shift_down = False

        # set up dispatch dictionaries for layer select handlers
        # for point select
        self.layerPSelHandler = {self.TypePoint: self.GetPointInLayer,
                                 self.TypeImage: self.GetImageInLayer,
                                 self.TypeText: self.GetTextInLayer,
                                 self.TypePoly: self.GetPolygonInLayer}

        # for box select
        self.layerBSelHandler = {self.TypePoint: self.GetBoxSelPointsInLayer,
                                 self.TypeImage: self.GetBoxSelImagesInLayer,
                                 self.TypeText: self.GetBoxSelTextsInLayer,
                                 self.TypePoly: self.GetBoxSelPolygonsInLayer}

        # bind event handlers
        self.Bind(wx.EVT_MOTION, self.OnMove)
        self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
        self.Bind(wx.EVT_LEFT_DCLICK, self.OnLeftDClick)
        self.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
        self.Bind(wx.EVT_MIDDLE_DOWN, self.OnMiddleDown)
        self.Bind(wx.EVT_MIDDLE_UP, self.OnMiddleUp)
        self.Bind(wx.EVT_RIGHT_DOWN, self.OnRightDown)
        self.Bind(wx.EVT_RIGHT_DCLICK, self.OnRightDClick)
        self.Bind(wx.EVT_RIGHT_UP, self.OnRightUp)
        self.Bind(wx.EVT_MOUSEWHEEL, self.OnMouseWheel)
        self.Bind(wx.EVT_ENTER_WINDOW, self.OnEnterWindow)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.OnLeaveWindow)

        # we also check KEY events, mostly for SHIFT key
        self.Bind(wx.EVT_KEY_DOWN, self.OnKeyDown)
        self.Bind(wx.EVT_KEY_UP, self.OnKeyUp)

        # set callback from Tile source object when tile(s) available
        self.tiles.SetAvailableCallback(self.OnTileAvailable)

        # set callback when parent resizes
        self.onSizeCallback = self.ResizeCallback

        # finally, use the tile level the user wants
        self.ZoomToLevel(self.level)

        # force a resize, which sets up the rest of the state
        # eventually calls ResizeCallback()
        self.OnSize()

    def OnTileAvailable(self, level, x, y, img, bmp):
        """Callback routine: tile level/x/y is available.

        level  the map zoom level the image is for
        x, y   tile coordinates of new tile
        img    tile image
        bmp    tile bitmap

        We don't use any of the above - just redraw the entire canvas.
        """

        self.Update()

    def OnEnterWindow(self, event):
        """Event handler when mouse enters widget."""

        pass

    def OnLeaveWindow(self, event):
        """Event handler when mouse leaves widget."""

        self.RaiseEventPosition(None, None)

    ######
    # "add a layer" routines
    ######

    def AddPointLayer(self, points, map_rel=True, visible=True,
                      show_levels=None, selectable=False,
                      name='<points_layer>', **kwargs):
        """Add a layer of points, map or view relative.

        points       iterable of point data:
                         (x, y[, attributes])
                     where x & y are either lon&lat (map) or x&y (view) coords
                     and attributes is an optional dictionary of attributes for
                     _each point_ with keys like:
                         'placement'  a placement string
                         'radius'     radius of point in pixels
                         'colour'     colour of point
                         'offset_x'   X offset
                         'offset_y'   Y offset
                         'data'       point user data object
        map_rel      points are map relative if True, else view relative
        visible      True if the layer is visible
        show_levels  list of levels at which layer is auto-shown (or None==all)
        selectable   True if select operates on this layer
        name         the 'name' of the layer - mainly for debug
        kwargs       a layer-specific attributes dictionary, has keys:
                         'placement'  a placement string
                         'radius'     radius of point in pixels
                         'colour'     colour of point
                         'offset_x'   X offset
                         'offset_y'   Y offset
                         'data'       point user data object
        """

        # merge global and layer defaults
        if map_rel:
            default_placement = kwargs.get('placement', self.DefaultPointPlacement)
            default_radius = kwargs.get('radius', self.DefaultPointRadius)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultPointColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultPointOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultPointOffsetY)
            default_data = kwargs.get('data', self.DefaultPointData)
        else:
            default_placement = kwargs.get('placement', self.DefaultPointViewPlacement)
            default_radius = kwargs.get('radius', self.DefaultPointViewRadius)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultPointViewColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultPointViewOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultPointViewOffsetY)
            default_data = kwargs.get('data', self.DefaultPointData)

        # create draw data iterable for draw method
        draw_data = []              # list to hold draw data

        for pt in points:
            if len(pt) == 3:
                (x, y, attributes) = pt
            elif len(pt) == 2:
                (x, y) = pt
                attributes = {}
            else:
                msg = ('Point data must be iterable of tuples: '
                       '(x, y[, dict])\n'
                       'Got: %s' % str(pt))
                raise Exception(msg)

            # plug in any required polygon values (override globals+layer)
            placement = attributes.get('placement', default_placement)
            radius = attributes.get('radius', default_radius)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            udata = attributes.get('data', default_data)

            # check values that can be wrong
            placement = placement.lower()
            if placement not in self.valid_placements:
                msg = ("Point placement value is invalid, got '%s'"
                       % str(placement))
                raise Exception(msg)

            # append another point to draw data list
            draw_data.append((float(x), float(y), placement,
                              radius, colour, offset_x, offset_y, udata))

        return self.AddLayer(self.DrawPointLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypePoint)

    def AddImageLayer(self, data, map_rel=True, visible=True,
                      show_levels=None, selectable=False,
                      name='<image_layer>', **kwargs):
        """Add a layer of images, map or view relative.

        data         list of (lon, lat, fname[, attributes]) (map_rel)
                     or list of (x, y, fname[, attributes]) (view relative)
                     attributes is a dictionary of attributes:
                         placement  a placement string
                         radius     object point radius
                         colour     object point colour
                         offset_x   X offset
                         offset_y   Y offset
                         data       image user data
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown (or None)
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       dictionary of extra params:
                         placement  string describing placement wrt hotspot
                         radius     object point radius
                         colour     object point colour
                         offset_x   hotspot X offset in pixels
                         offset_y   hotspot Y offset in pixels
                         data       image user data

        The hotspot is placed at (lon, lat) or (x, y).  'placement' controls
        where the image is displayed relative to the hotspot.
        """

        # merge global and layer defaults
        if map_rel:
            default_placement = kwargs.get('placement', self.DefaultImagePlacement)
            default_radius = kwargs.get('radius', self.DefaultImageRadius)
            default_colour = kwargs.get('colour', self.DefaultImageColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultImageOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultImageOffsetY)
            default_data = kwargs.get('data', self.DefaultImageData)
        else:
            default_placement = kwargs.get('placement', self.DefaultImageViewPlacement)
            default_radius = kwargs.get('radius', self.DefaultImageViewRadius)
            default_colour = kwargs.get('colour', self.DefaultImageViewColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultImageViewOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultImageViewOffsetY)
            default_data = kwargs.get('data', self.DefaultImageViewData)

        # define cache variables for the image informtion
        # used to minimise file access - just caches previous file informtion
        fname_cache = None
        bmp_cache = None
        w_cache = None
        h_cache = None

        # load all image files, convert to bitmaps, create draw_data iterable
        draw_data = []
        for d in data:
            if len(d) == 4:
                (lon, lat, fname, attributes) = d
            elif len(d) == 3:
                (lon, lat, fname) = d
                attributes = {}
            else:
                msg = ('Image data must be iterable of tuples: '
                       '(x, y, fname[, dict])\nGot: %s' % str(d))
                raise Exception(msg)

            # get image specific values, if any
            placement = attributes.get('placement', default_placement)
            radius = attributes.get('radius', default_radius)
            colour = attributes.get('colour', default_colour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            udata = attributes.get('data', None)

            if fname == fname_cache:
                bmap = bmp_cache
                w = w_cache
                h = h_cache
            else:
                fname_cache = fname
                img = wx.Image(fname, wx.BITMAP_TYPE_ANY)
                bmp_cache = bmap = img.ConvertToBitmap()
                (w, h) = bmap.GetSize()
                w_cache = w
                h_cache = h

            # check values that can be wrong
            placement = placement.lower()
            if placement not in self.valid_placements:
                msg = ("Image placement value is invalid, got '%s'"
                       % str(placement))
                raise Exception(msg)

            draw_data.append((float(lon), float(lat), bmap, w, h, placement,
                              offset_x, offset_y, radius, colour, udata))

        return self.AddLayer(self.DrawImageLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypeImage)

    def AddTextLayer(self, text, map_rel=True, visible=True, show_levels=None,
                     selectable=False, name='<text_layer>', **kwargs):
        """Add a text layer to the map or view.

        text         list of sequence of (lon, lat, text[, dict]) coordinates
                     (optional 'dict' contains point-specific attributes)
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       a dictionary of changeable text attributes
                         (placement, radius, fontname, fontsize, colour, data)
                     these supply any data missing in 'data'
        """

        # merge global and layer defaults
        if map_rel:
            default_placement = kwargs.get('placement', self.DefaultTextPlacement)
            default_radius = kwargs.get('radius', self.DefaultTextRadius)
            default_fontname = kwargs.get('fontname', self.DefaultTextFontname)
            default_fontsize = kwargs.get('fontsize', self.DefaultTextFontSize)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultTextColour)
            default_textcolour = self.get_i18n_kw(kwargs,
                                                  ('textcolour', 'textcolor'),
                                                  self.DefaultTextTextColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultTextOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultTextOffsetY)
            default_data = kwargs.get('data', self.DefaultTextData)
        else:
            default_placement = kwargs.get('placement', self.DefaultTextViewPlacement)
            default_radius = kwargs.get('radius', self.DefaultTextViewRadius)
            default_fontname = kwargs.get('fontname', self.DefaultTextViewFontname)
            default_fontsize = kwargs.get('fontsize', self.DefaultTextViewFontSize)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultTextViewColour)
            default_textcolour = self.get_i18n_kw(kwargs,
                                                  ('textcolour', 'textcolor'),
                                                  self.DefaultTextViewTextColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultTextViewOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultTextViewOffsetY)
            default_data = kwargs.get('data', self.DefaultTextData)

        # create data iterable ready for drawing
        draw_data = []
        for t in text:
            if len(t) == 4:
                (lon, lat, tdata, attributes) = t
            elif len(t) == 3:
                (lon, lat, tdata) = t
                attributes = {}
            else:
                msg = ('Text data must be iterable of tuples: '
                       '(lon, lat, text, [dict])\n'
                       'Got: %s' % str(t))
                raise Exception(msg)

            # plug in any required defaults
            placement = attributes.get('placement', default_placement)
            radius = attributes.get('radius', default_radius)
            fontname = attributes.get('fontname', default_fontname)
            fontsize = attributes.get('fontsize', default_fontsize)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            textcolour = self.get_i18n_kw(attributes,
                                          ('textcolour', 'textcolor'),
                                          default_textcolour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            udata = attributes.get('data', default_data)

            # check values that can be wrong
            placement = placement.lower()
            if placement not in self.valid_placements:
                msg = ("Text placement value is invalid, got '%s'"
                       % str(placement))
                raise Exception(msg)

            draw_data.append((float(lon), float(lat), tdata, placement.lower(),
                              radius, colour, textcolour, fontname, fontsize,
                              offset_x, offset_y, udata))

        return self.AddLayer(self.DrawTextLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypeText)

    def AddPolygonLayer(self, data, map_rel=True, visible=True,
                        show_levels=None, selectable=False,
                        name='<polygon_layer>', **kwargs):
        """Add a layer of polygon data to the map.

        data         iterable of polygon tuples:
                         (<iter>[, attributes])
                     where <iter> is another iterable of (x, y) tuples and
                     attributes is a dictionary of polygon attributes:
                         placement   a placement string (view-relative only)
                         width       width of polygon edge lines
                         colour      colour of edge lines
                         close       if True closes polygon
                         filled      polygon is filled (implies closed)
                         fillcolour  fill colour
                         offset_x    X offset
                         offset_y    Y offset
                         data        polygon user data object
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown (or None)
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       extra keyword args, layer-specific:
                         placement   placement string (view-rel only)
                         width       width of polygons in pixels
                         colour      colour of polygon edge lines
                         close       True if polygon is to be closed
                         filled      if True, fills polygon
                         fillcolour  fill colour
                         offset_x    X offset
                         offset_y    Y offset
                         data        polygon user data object
        """

        # merge global and layer defaults
        if map_rel:
            default_placement = kwargs.get('placement',
                                           self.DefaultPolyPlacement)
            default_width = kwargs.get('width', self.DefaultPolyWidth)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultPolyColour)
            default_close = kwargs.get('closed', self.DefaultPolyClose)
            default_filled = kwargs.get('filled', self.DefaultPolyFilled)
            default_fillcolour = self.get_i18n_kw(kwargs,
                                                  ('fillcolour', 'fillcolor'),
                                                  self.DefaultPolyFillcolour)
            default_offset_x = kwargs.get('offset_x', self.DefaultPolyOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultPolyOffsetY)
            default_data = kwargs.get('data', self.DefaultPolyData)
        else:
            default_placement = kwargs.get('placement',
                                           self.DefaultPolyViewPlacement)
            default_width = kwargs.get('width', self.DefaultPolyViewWidth)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultPolyViewColour)
            default_close = kwargs.get('closed', self.DefaultPolyViewClose)
            default_filled = kwargs.get('filled', self.DefaultPolyViewFilled)
            default_fillcolour = self.get_i18n_kw(kwargs,
                                                  ('fillcolour', 'fillcolor'),
                                                  self.DefaultPolyViewFillcolour)
            default_offset_x = kwargs.get('offset_x', self.DefaultPolyViewOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultPolyViewOffsetY)
            default_data = kwargs.get('data', self.DefaultPolyViewData)

        # create draw_data iterable
        draw_data = []
        for d in data:
            if len(d) == 2:
                (p, attributes) = d
            elif len(d) == 1:
                p = d
                attributes = {}
            else:
                msg = ('Polygon data must be iterable of tuples: '
                       '(poly, [attributes])\n'
                       'Got: %s' % str(d))
                raise Exception(msg)

            # get polygon attributes
            placement = attributes.get('placement', default_placement)
            width = attributes.get('width', default_width)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            close = attributes.get('closed', default_close)
            filled = attributes.get('filled', default_filled)
            if filled:
                close = True
            fillcolour = self.get_i18n_kw(attributes,
                                          ('fillcolour', 'fillcolor'),
                                          default_fillcolour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            udata = attributes.get('data', default_data)

            # if polygon is to be filled, ensure closed
            if close:
                p = list(p)     # must get a *copy*
                p.append(p[0])

            # check values that can be wrong
            placement = placement.lower()
            if placement not in self.valid_placements:
                msg = ("Polygon placement value is invalid, got '%s'"
                       % str(placement))
                raise Exception(msg)

            draw_data.append((p, placement, width, colour, close,
                              filled, fillcolour, offset_x, offset_y, udata))

        return self.AddLayer(self.DrawPolygonLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypePoly)

    def AddLayer(self, painter, data, map_rel, visible, show_levels,
                 selectable, name, type):
        """Add a generic layer to the system.

        painter      the function used to paint the layer
        data         actual layer data (depends on layer type)
        map_rel      True if points are map relative, else view relative
        visible      True if layer is to be immediately shown, else False
        show_levels  list of levels at which to auto-show the layer
        selectable   True if select operates on this layer
        name         name for this layer
        type         flag for layer 'type'

        Returns unique ID of the new layer.
        """

        # get layer ID
        id = self.next_layer_id
        self.next_layer_id += 1

        # prepare the show_level value
        if show_levels is None:
            show_levels = range(self.min_level, self.max_level + 1)[:]

        # create layer, add unique ID to Z order list
        l = _Layer(id=id, painter=painter, data=data, map_rel=map_rel,
                   visible=visible, show_levels=show_levels,
                   selectable=selectable, name=name, type=type)

        self.layer_mapping[id] = l
        self.layer_z_order.append(id)

        # force display of new layer if it's visible
        if visible:
            self.Update()

        return id

    ######
    # Layer manipulation routines.
    ######

    def ShowLayer(self, id):
        """Show a layer.

        id  the layer id
        """

        self.layer_mapping[id].visible = True
        self.Update()

    def HideLayer(self, id):
        """Hide a layer.

        id  the layer id
        """

        self.layer_mapping[id].visible = False
        self.Update()

    def DeleteLayer(self, id):
        """Delete a layer.

        id  the layer id
        """

        # just in case we got None
        if id:
            # see if what we are about to remove might be visible
            layer = self.layer_mapping[id]
            visible = layer.visible

            del layer
            self.layer_z_order.remove(id)

            # if layer was visible, refresh display
            if visible:
                self.Update()

    def SetLayerShowLevels(self, id, show_levels=None):
        """Update the show_levels list for a layer.

        id           ID of the layer we are going to update
        show_levels  new layer show list
        """

        # just in case we got None
        if id:
            layer = self.layer_mapping[id]

            # prepare the show_level value
            if show_levels is None:
                show_levels = range(self.min_level, self.max_level + 1)[:]

            layer.show_levels = show_levels

            # if layer was visible, refresh display
            if visible:
                self.Update()

    def SetLayerSelectable(self, id, selectable=False):
        """Update the .selectable attribute for a layer.

        id          ID of the layer we are going to update
        selectable  new .selectable attribute value (True or False)
        """

        # just in case id is None
        if id:
            layer = self.layer_mapping[id]
            layer.selectable = selectable

    ######
    # Play with layers Z order
    ######

    def PushLayerToBack(self, id):
        """Make layer specified be drawn at back of Z order.

        id  ID of the layer to push to the back
        """

        self.layer_z_order.remove(id)
        self.layer_z_order.insert(0, id)
        self.Update()

    def PopLayerToFront(self, id):
        """Make layer specified be drawn at front of Z order.

        id  ID of the layer to pop to the front
        """

        self.layer_z_order.remove(id)
        self.layer_z_order.append(id)
        self.Update()

    def PlaceLayerBelowLayer(self, id, top_id):
        """Place a layer so it will be drawn behind another layer.

        id      ID of layer to place underneath 'top_id'
        top_id  ID of layer to be drawn *above* 'id'
        """

        self.layer_z_order.remove(id)
        i = self.layer_z_order.index(top_id)
        self.layer_z_order.insert(i, id)
        self.Update()

    ######
    # Layer drawing routines
    ######

    def DrawPointLayer(self, dc, data, map_rel):
        """Draw a points layer.

        dc       the device context to draw on
        data     an iterable of point tuples:
                     (x, y, place, radius, colour, x_off, y_off, udata)
        map_rel  points relative to map if True, else relative to view
        """

        # allow transparent colours
        dc = wx.GCDC(dc)

        # get correct pex function
        pex = self.PexPointView
        if map_rel:
            pex = self.PexPoint

        # draw points on map/view
        for (x, y, place, radius, colour, x_off, y_off, udata) in data:
            (pt, ex) = pex(place, (x,y), x_off, y_off, radius)
            if ex and radius:  # don't draw if not on screen or zero radius
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                (x, _, y, _) = ex
                dc.DrawCircle(x+radius, y+radius, radius)

    def DrawImageLayer(self, dc, images, map_rel):
        """Draw an image Layer on the view.

        dc       the device context to draw on
        images   a sequence of image tuple sequences
                   (x,y,bmap,w,h,placement,offset_x,offset_y,idata)
        map_rel  points relative to map if True, else relative to view
        """

        # allow transparent colours
        dc = wx.GCDC(dc)

        # get correct pex function
        pex = self.PexExtentView
        if map_rel:
            pex = self.PexExtent

        # draw the images
        for (lon, lat, bmap, w, h, place,
                 x_off, y_off, radius, colour, idata) in images:
            (pt, ex) = pex(place, (lon, lat), x_off, y_off, w, h)
            if ex:
                (ix, _, iy, _) = ex
                dc.DrawBitmap(bmap, ix, iy, False)

            if pt and radius:
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                (px, py) = pt
                dc.DrawCircle(px, py, radius)

    def DrawTextLayer(self, dc, text, map_rel):
        """Draw a text Layer on the view.

        dc       the device context to draw on
        text     a sequence of tuples:
                     (lon, lat, tdata, placement, radius, colour, fontname,
                      fontsize, offset_x, offset_y, tdata)
        map_rel  points relative to map if True, else relative to view
        """

        # we need the size of the DC
        dc = wx.GCDC(dc)		# allow transparent colours

        # get correct pex function for mode (map/view)
        pex = self.PexExtentView
        if map_rel:
            pex = self.PexExtent

        # draw text on map/view
        last_setfont = None
        for (lon, lat, tdata, place, radius, colour,
                textcolour, fontname, fontsize, x_off, y_off, data) in text:

            # set font characteristics so we calculate text width/height
            dc.SetTextForeground(textcolour)
            if last_setfont != (fontname, fontsize):
                font = wx.Font(fontsize, wx.SWISS, wx.NORMAL, wx.NORMAL,
                               False, fontname)
                dc.SetFont(font)
                last_setfont = (fontname, fontsize)

            (w, h, _, _) = dc.GetFullTextExtent(tdata)

            # get point + extent information (each can be None if off-view)
            (pt, ex) = pex(place, (lon, lat), x_off, y_off, w, h)
            if ex:
                (lx, _, ty, _) = ex
                dc.DrawText(tdata, lx, ty)

            if pt and radius:
                (x, y) = pt
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                dc.DrawCircle(x, y, radius)

    def DrawPolygonLayer(self, dc, data, map_rel):
        """Draw a polygon layer.

        dc       the device context to draw on
        data     an iterable of polygon tuples:
                     (p, placement, width, colour, closed,
                      filled, fillcolour, offset_x, offset_y, udata)
                 where p is an iterable of points: (x, y)
        map_rel  points relative to map if True, else relative to view
        """

        # allow transparent colours
        dc = wx.GCDC(dc)

        # get the correct pex function for mode (map/view)
        pex = self.PexPolyView
        if map_rel:
            pex = self.PexPoly

        # draw polygons
        for (p, place, width, colour, closed,
                 filled, fillcolour, x_off, y_off, udata) in data:
            (poly, extent) = pex(place, p, x_off, y_off)
            if poly:
                dc.SetPen(wx.Pen(colour, width=width))

                if filled:
                    dc.SetBrush(wx.Brush(fillcolour))
                else:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)

                if closed:
                    dc.DrawPolygon(poly)
                else:
                    dc.DrawLines(poly)

    ######
    # Positioning methods
    ######

    def GotoPosition(self, geo):
        """Set view to centre on a geo position in the current level.

        geo  a tuple (xgeo,ygeo) to centre view on

        Sets self.view_offset_x and self.view_offset_y and then calls
        RecalcViewLimits(), redraws widget.
        """

        # get fractional tile coords of required centre of view
        (xtile, ytile) = self.tiles.Geo2Tile(geo)

        # now calculate view offsets, top, left, bottom and right
        half_width = self.view_width / 2
        centre_pixels_from_map_left = int(xtile * self.tile_size_x)
        self.view_offset_x = centre_pixels_from_map_left - half_width

        half_height = self.view_height / 2
        centre_pixels_from_map_top = int(ytile * self.tile_size_y)
        self.view_offset_y = centre_pixels_from_map_top - half_height

        # set the left/right/top/bottom lon/lat extents and redraw view
        self.RecalcViewLimits()
        self.Update()

    def GotoLevelAndPosition(self, level, geo):
        """Goto a map level and set view to centre on a position.

        level  the map level to use
        geo    a tuple (xgeo,ygeo) to centre view on

        Does nothing if we can't use desired level.
        """

        if self.ZoomToLevel(level):
            self.GotoPosition(geo)

    def ZoomToArea(self, geo, size):
        """Set view to level and position to view an area.

        geo   a tuple (xgeo,ygeo) to centre view on
        size  a tuple (width,height) of area in degrees

        Centre an area and zoom to view such that the area will fill
        approximately 50% of width or height, whichever is greater.

        Use the ppd_x and ppd_y values in the level 'tiles.info' file.
        """

        # unpack area width/height (degrees)
        (awidth, aheight) = size

        # step through levels (smallest first) and check view size (degrees)
        for l in self.tiles.levels:
            level = l
            (_, _, ppd_x, ppd_y) = self.tiles.getInfo(l)
            view_deg_width = self.view_width / ppd_x
            view_deg_height = self.view_height / ppd_y

            # if area >= 50% of view, finished
            if awidth >= view_deg_width / 2 or aheight >= view_deg_height / 2:
                break

        self.GotoLevelAndPosition(level, geo)

    ######
    # Convert between geo and view coordinates
    ######

    def Geo2View(self, geo):
        """Convert a geo coord to view.

        geo  tuple (xgeo, ygeo)

        Return a tuple (xview, yview) in view coordinates.
        Assume point is in view.
        """

        (tx, ty) = self.tiles.Geo2Tile(geo)
        return ((tx * self.tiles.tile_size_x) - self.view_offset_x,
                (ty * self.tiles.tile_size_y) - self.view_offset_y)


    def Geo2ViewMasked(self, geo):
        """Convert a geo (lon+lat) position to view pixel coords.

        geo  tuple (xgeo, ygeo)

        Return a tuple (xview, yview) of point if on-view,or None
        if point is off-view.
        """

        (xgeo, ygeo) = geo

        if (self.view_llon <= xgeo <= self.view_rlon and
                self.view_blat <= ygeo <= self.view_tlat):
            return self.Geo2View(geo)

        return None

    ######
    # PEX - Point & EXtension.
    #
    # These functions encapsulate the code that finds the extent of an object.
    # They all return a tuple (point, extent) where 'point' is the placement
    # point of an object (or list of points for a polygon) and an 'extent'
    # tuple (lx, rx, ty, by) [left, right, top, bottom].
    ######

    def PexPoint(self, place, geo, x_off, y_off, radius):
        """Given a point object (geo coords) get point/extent in view coords.

        place         placement string
        geo           point position tuple (xgeo, ygeo)
        x_off, y_off  X and Y offsets

        Return a tuple of point and extent origins (point, extent) where 'point'
        is (px, py) and extent is (elx, erx, ety, eby) (both in view coords).
        Return None for either or both if off-view.

        The 'extent' here is the extent of the point+radius.
        """

        # get point view coords
        (xview, yview) = self.Geo2View(geo)
        point = self.point_placement(place, xview, yview, x_off, y_off)
        (px, py) = point

        # extent = (left, right, top, bottom) in view coords
        elx = px - radius
        erx = px + radius
        ety = py - radius
        eby = py + radius
        extent = (elx, erx, ety, eby)

        # decide if point and extent are off-view
        if px < 0 or px > self.view_width or py < 0 or py > self.view_height:
            point = None

        if erx < 0 or elx > self.view_width or eby < 0 or ety > self.view_height:
            # no extent if ALL of extent is off-view
            extent = None

        return (point, extent)

    def PexPointView(self, place, view, x_off, y_off, radius):
        """Given a point object (view coords) get point/extent in view coords.

        place         placement string
        view          point position tuple (xview, yview)
        x_off, y_off  X and Y offsets

        Return a tuple of point and extent origins (point, extent) where 'point'
        is (px, py) and extent is (elx, erx, ety, eby) (both in view coords).
        Return None for either or both if off-view.

        The 'extent' here is the extent of the point+radius.
        """

        # get point view coords and perturb point to placement
        (xview, yview) = view
        point = self.point_placement(place, xview, yview, x_off, y_off,
                                     self.view_width, self.view_height)
        (px, py) = point

        # extent = (left, right, top, bottom) in view coords
        elx = px - radius
        erx = px + radius
        ety = py - radius
        eby = py + radius
        extent = (elx, erx, ety, eby)

        # decide if point and extent are off-view
        if (px < 0 or px > self.view_width
                or py < 0 or py > self.view_height):
            view = None

        if erx < 0 or elx > self.view_width or eby < 0 or ety > self.view_height:
            # no extent if ALL of extent is off-view
            extent = None

        return (point, extent)

    def PexExtent(self, place, geo, x_off, y_off, w, h):
        """Given an extent object convert point/extent coords to view coords.

        place         placement string
        geo           point position tuple (xgeo, ygeo)
        x_off, y_off  X and Y offsets
        w, h          width and height of extent in pixels

        Return a tuple of point and extent origins (point, extent) where 'point'
        is (px, py) and extent is (elx, erx, ety, eby) (both in view coords).
        Return None if point is off-view.

        An extent object can be either an image object or a text object.
        """

        # get point view coords
        point = self.Geo2View(geo)
        (px, py) = point

        # extent = (left, right, top, bottom) in view coords
        extent = self.ViewExtent(place, point, w, h, x_off, y_off)
        (elx, erx, ety, eby) = extent

        # decide if point and extent are off-view
        if px < 0 or px > self.view_width or py < 0 or py > self.view_height:
            point = None

        if erx < 0 or elx > self.view_width or eby < 0 or ety > self.view_height:
            # no extent if ALL of extent is off-view
            extent = None

        return (point, extent)

    def PexExtentView(self, place, view, x_off, y_off, w, h):
        """Given a view object convert point/extent coords to view coords.

        place         placement string
        view          point position tuple (xview, yview)
        x_off, y_off  X and Y offsets
        w, h          width and height of extent in pixels

        Return a tuple of point and extent origins (point, extent) where 'point'
        is (px, py) and extent is (elx, erx, ety, eby) (both in view coords).
        Either point or extent is None if object off-view.

        Takes size of extent object into consideration.
        """

        # get point view coords and perturb point to placement origin
        (xview, yview) = view
        point = self.point_placement(place, xview, yview, 0, 0,
                                     self.view_width, self.view_height)

        # get point view coords (X and Y)
        (px, py) = view

        # extent = (left, right, top, bottom) in view coords
        extent = self.ViewExtent(place, view, w, h, x_off, y_off,
                                 self.view_width, self.view_height)
        (elx, erx, ety, eby) = extent

        # decide if point and extent are off-view
        if px < 0 or px > self.view_width or py < 0 or py > self.view_height:
            view = None

        if erx < 0 or elx > self.view_width or eby < 0 or ety > self.view_height:
            # no extent if ALL of extent is off-view
            extent = None

        return (point, extent)

    def PexPoly(self, place, poly, x_off, y_off):
        """Given a polygon object (geo coords) get point/extent in view coords.

        place         placement string
        poly          list of point position tuples (xgeo, ygeo)
        x_off, y_off  X and Y offsets

        Return a tuple of point and extent origins (point, extent) where 'point'
        is a list of (px, py) and extent is (elx, erx, ety, eby) (both in view
        coords).  Return None for either or both if off-view.
        """

        # get polygon points in perturbed view coordinates
        view = []
        for geo in poly:
            (xview, yview) = self.Geo2View(geo)
            point = self.point_placement(place, xview, yview, x_off, y_off)
            view.append(point)

        # get extent - max/min x and y
        # extent = (left, right, top, bottom) in view coords
        elx = min(view, key=lambda x: x[0])[0]
        erx = max(view, key=lambda x: x[0])[0]
        ety = min(view, key=lambda x: x[1])[1]
        eby = max(view, key=lambda x: x[1])[1]
        extent = (elx, erx, ety, eby)

        # decide if polygon or extent are off-view
        res_pt = None
        res_ex = None
        for (px, py) in view:
            if ((px >= 0 and px < self.view_width)
                    and (py >= 0 and py < self.view_height)):
                res_pt = view
                res_ex = extent
                break

        return (res_pt, res_ex)

    def PexPolyView(self, place, poly, x_off, y_off):
        """Given a polygon object (view coords) get point/extent in view coords.

        place         placement string
        poly          list of point position tuples (xview, yview)
        x_off, y_off  X and Y offsets

        Return a tuple of point and extent origins (point, extent) where 'point'
        is a list of (px, py) and extent is (elx, erx, ety, eby) (both in view
        coords).  Return None for either or both if off-view.
        """

        # get polygon points in view coordinates
        view = []
        for (xview, yview) in poly:
            point = self.point_placement(place, xview, yview, x_off, y_off,
                                         self.view_width, self.view_height)
            view.append(point)

        # get extent - max/min x and y
        # extent = (left, right, top, bottom) in view coords
        elx = min(view, key=lambda x: x[0])[0]
        erx = max(view, key=lambda x: x[0])[0]
        ety = min(view, key=lambda x: x[1])[1]
        eby = max(view, key=lambda x: x[1])[1]
        extent = (elx, erx, ety, eby)

        # decide if polygon or extent are off-view
        res_pt = None
        res_ex = None
        for (px, py) in view:
            if ((px >= 0 and px < self.view_width)
                    and (py >= 0 and py < self.view_height)):
                res_pt = view
                res_ex = extent
                break

        return (res_pt, res_ex)

    ######
    # GUI stuff
    ######

    def OnMove(self, event):
        """Handle a mouse move (map drag or rectangle select).

        event  the mouse move event

        If SHIFT key is down, do rectangle select.
        Otherwise pan the map if we are dragging.
        """

        # for windows, set focus onto pyslip window
        # linux seems to do this automatically
        if sys.platform == 'win32' and self.FindFocus() != self:
            self.SetFocus()

        # get current mouse position
        mouse_view = event.GetPositionTuple()
        mouse_map = self.View2Geo(mouse_view)
        self.RaiseEventPosition(mouse_map, mouse_view)

        if event.Dragging() and event.LeftIsDown():
            (x, y) = mouse_view

            # are we doing box select?
            if self.is_box_select:
                # set select box point 2 at mouse position
                (self.sbox_w, self.sbox_h) = (x - self.sbox_1_x,
                                              y - self.sbox_1_y)
            elif not self.last_drag_x is None:
                # no, just a map drag
                self.was_dragging = True
                dx = self.last_drag_x - x
                dy = self.last_drag_y - y

                # move the map in the view
                self.view_offset_x += dx
                self.view_offset_y += dy

                # limit drag at edges of map
                if self.map_width > self.view_width:
                    # if map > view, don't allow edge to show background
                    if self.view_offset_x < 0:
                        self.view_offset_x = 0
                    elif self.view_offset_x > self.max_x_offset:
                        self.view_offset_x = self.max_x_offset
                else:
                    # else map < view, centre X
                    self.view_offset_x = (self.map_width
                                          - self.view_width) / 2

                if self.map_height > self.view_height:
                    # if map > view, don't allow edge to show background
                    if self.view_offset_y < 0:
                        self.view_offset_y = 0
                    elif self.view_offset_y > self.max_y_offset:
                        self.view_offset_y = self.max_y_offset
                else:
                    # else map < view, centre Y
                    self.view_offset_y = (self.map_height
                                          - self.view_height) / 2

                # adjust remembered X,Y
                self.last_drag_x = x
                self.last_drag_y = y

                self.RecalcViewLimits()

            # redraw client area
            self.Update()

    def OnKeyDown(self, event):
        if event.m_keyCode == wx.WXK_SHIFT:
            self.shift_down = True
            self.default_cursor = wx.CURSOR_CROSS
            self.SetCursor(wx.StockCursor(wx.CURSOR_CROSS))

    def OnKeyUp(self, event):
        if event.m_keyCode == wx.WXK_SHIFT:
            self.shift_down = False
            self.default_cursor = wx.CURSOR_DEFAULT
            self.SetCursor(wx.StockCursor(wx.CURSOR_DEFAULT))


    def OnLeftDown(self, event):
        """Left mouse button down. Prepare for possible drag."""

        click_posn = event.GetPositionTuple()

        if self.shift_down:
            self.is_box_select = True
            (self.sbox_w, self.sbox_h) = (0, 0)
            (self.sbox_1_x, self.sbox_1_y) = click_posn
        else:
            self.is_box_select = False
            self.SetCursor(wx.StockCursor(wx.CURSOR_HAND))
            (self.last_drag_x, self.last_drag_y) = click_posn
        event.Skip()

    def OnLeftUp(self, event):
        """Left mouse button up.

        Could be end of a drag or point or box selection.  If it's the end of
        a drag we don't do a lot.  If a selection we process that.
        """

        log('OnLeftUp: entered')

        # turn off any dragging
        self.last_drag_x = self.last_drag_y = None

        # if required, ignore this event
        if self.ignore_next_up:
            self.ignore_next_up = False
            return

        # cursor back to normal
        self.SetCursor(wx.StockCursor(self.default_cursor))

        # we need a repaint to remove any selection box, but NOT YET!
        delayed_paint = self.sbox_1_x       # True if box select active

        # if any layers interested, inform of possible select
        if not self.was_dragging:
            if self.is_box_select:
                # get canonical selection box in view coordinates
                (ll_vx, ll_vy, tr_vx, tr_vy) = self.sel_box_canonical()

                # selection box corners in tile coords
                ll_tx = float(ll_vx+self.view_offset_x) / self.tile_size_x
                ll_ty = float(ll_vy+self.view_offset_y) / self.tile_size_y
                tr_tx = float(tr_vx+self.view_offset_x) / self.tile_size_x
                tr_ty = float(tr_vy+self.view_offset_y) / self.tile_size_y

                # selection box in geo coords
                ll_g = self.tiles.Tile2Geo((ll_tx, ll_ty))
                tr_g = self.tiles.Tile2Geo((tr_tx, tr_ty))

                # check each layer for a box select event
                # we work on a copy as user response could change order
                for id in self.layer_z_order[:]:
                    l = self.layer_mapping[id]
                    # if layer visible and selectable
                    if l.selectable and l.visible:
                        if l.map_rel:
                            # map-relative, get all points selected (if any)
                            sel = self.layerBSelHandler[l.type](l, ll_g, tr_g)
                        else:
                            # view-relative
                            sel = self.layerBSelHandler[l.type](l,
                                                                (ll_vx, ll_vy),
                                                                (tr_vx, tr_vy))
                        log('OnLeftUp: BOX sel=%s' % str(sel))
                        self.RaiseEventBoxSelect(layer=l, selection=sel)

                        # user code possibly updated screen
                        delayed_paint = True
                self.is_box_select = False
            else:
                log('OnLeftUp: single selection?')

                # possible point selection, get click point in view coords
                clickpt_v = event.GetPositionTuple()

                # get click point in geo coords
                clickpt_g = self.View2Geo(clickpt_v)

                # check each layer for a point select callback
                # we work on a copy as user callback could change order
                for id in self.layer_z_order[:]:
                    l = self.layer_mapping[id]
                    # if layer visible and selectable
                    if l.selectable and l.visible:
                        if l.map_rel:
                            sel = self.layerPSelHandler[l.type](l, clickpt_g)
                        else:
                            sel = self.layerPSelHandler[l.type](l, clickpt_v)
                        log('OnLeftUp: SINGLE sel=%s' % str(sel))
                        self.RaiseEventSelect(mposn=clickpt_g, vposn=clickpt_v,
                                              layer=l, selection=sel)
                        # user code possibly updated screen
                        delayed_paint = True

        # turn off drag
        self.was_dragging = False

        # turn off box selection mechanism
        self.is_box_select = False
        self.sbox_1_x = self.sbox_1_y = None

        # force PAINT event if required
        if delayed_paint:
            self.Update()

    def OnLeftDClick(self, event):
        """Left mouse button double-click.

        Zoom in (if possible).
        Zoom out (if possible) if shift key is down.
        """

        # ignore next Left UP event
        self.ignore_next_up = True

        # FIXME: should ignore double-click off the map, but within view
        # FIXME: a possible workaround is to limit minimum view level

        # get view coords of mouse double click, want same centre afterwards
        vposn = event.GetPositionTuple()
        gposn = self.View2Geo(vposn)

        if self.shift_down:
            # zoom out if shift key also down
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut(gposn)
        else:
            # zoom in
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn(gposn)

    def OnMiddleDown(self, event):
        """Middle mouse button down.  Do nothing in this version."""

        pass

    def OnMiddleUp(self, event):
        """Middle mouse button up.  Do nothing in this version."""

        pass

    def OnRightDown(self, event):
        """Right mouse button down. Prepare for right select (no drag)."""

        click_posn = event.GetPositionTuple()

        if self.shift_down:
            self.is_box_select = True
            self.SetCursor(wx.StockCursor(wx.CURSOR_CROSS))
            (self.sbox_w, self.sbox_h) = (0, 0)
            (self.sbox_1_x, self.sbox_1_y) = click_posn
        event.Skip()

    def OnRightUp(self, event):
        """Right mouse button up.

        Note that when we iterate through the layer_z_order list we must
        iterate on a *copy* as the user select process can modify
        self.layer_z_order.

        THIS CODE HASN'T BEEN LOOKED AT IN A LONG, LONG TIME.
        """

        if self.ignore_next_right_up:
            self.ignore_next_right_up = False
            return

        self.SetCursor(wx.StockCursor(wx.CURSOR_DEFAULT))

        # we need a repaint to remove any selection box, but NOT YET!
        delayed_paint = self.sbox_1_x       # True if box select active

        # if any layers interested, inform of possible select
        if self.is_box_select:
            # possible box selection
            ll_x = (self.sbox_1_x + self.view_offset_x) / self.tile_size_x
            ll_y = (self.sbox_1_y + self.view_offset_y) / self.tile_size_y

            ll_g = self.tiles.Tile2Geo((ll_x, ll_y))
            tr_g = self.tiles.Tile2Geo((ll_x + self.sbox_w, ll_y + self.sbox_h))

            # check each layer for a box select event
            # we work on a copy as user response could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                if l.selectable and l.visible:   # and l.event_box_select:
                    if l.map_rel:
                        # map-relative, get all points selected (if any)
                        pts = self.layerBSelHandler[l.type](l, ll_g, tr_g)
                    else:
                        # view-relative
                        pts = self.layerBSelHandler[l.type](l,
                                                            (ll_x, ll_y),
                                                            (ll_x+self.sbox_w,
                                                             ll_y+self.sbox_h))
                    self.RaiseEventSelect(EventRightBoxSelect, layer=l, selection=pts)

                    # user code possibly updated screen
                    delayed_paint = True
            self.is_box_select = False
        else:
            # possible point selection, get tile coords
            click_v = event.GetPositionTuple()
            (click_vx, click_vy) = click_v
            click_vx += self.view_offset_x
            click_vy += self.view_offset_y
            click_g = self.tiles.Tile2Geo((click_vx, click_vy))
            # FIXME: do we REALLY need tile coords?

            # check each layer for a point select callback
            # we work on a copy as user callback could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                # if layer visible, selectable and there is a callback
                if l.selectable and l.visible:
                    if l.map_rel:
                        pt = self.layerPSelHandler[l.type](l, click_g)
                    else:
                        pt = self.layerPSelHandler[l.type](l, click_v)
                    self.RaiseEventSelect(EventRightSelect, layer=l, selection=pt,
                                          mposn=click_g, vposn=click_v)

                    # user code possibly updated screen
                    delayed_paint = True

        # turn off box selection mechanism
        self.is_box_select = False
        self.sbox_1_x = self.sbox_1_y = None

        # force PAINT event to remove selection box (if required)
        if delayed_paint:
            self.Update()

    def OnRightDClick(self, event):
        """Right mouse button double-click."""

        # ignore next RIGHT UP event
        self.ignore_next_right_up = True

    def OnMouseWheel(self, event):
        """Mouse wheel event."""

        # get centre of view in map coords, want same centre afterwards
        x = self.view_width / 2
        y = self.view_height / 2
        gposn = self.View2Geo((x, y))

        # determine which way to zoom, & *can* we zoom?
        if event.GetWheelRotation() > 0:
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn(gposn)
        else:
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut(gposn)

    ######
    # Method that overrides _BufferedCanvas.Draw() method.
    # This code does the actual drawing of tiles, layers, etc.
    ######

    def Draw(self, dc):
        """Do actual map tile and layers drawing.
        Overrides the _BufferedCanvas.draw() method.

        dc  device context to draw on

        The idea is to create 4 things that define the tiles to be drawn and
        where to draw them:
            x_pix_start  view pixel coord of left side of top-left tile
            y_pix_start  view pixel coord of top side of top-left tole
            row_list     list (top -> bottom) of tile rows
            col_list     list (left -> right) of tile columns

        Note that (x_pix_start, y_pix_start) will typically be OUTSIDE the view
        if the view is smaller than the map.
        """

        # figure out how to draw tiles
        if self.view_offset_x < 0:
            # View > Map in X - centre in X direction
            col_list = range(self.tiles.num_tiles_x)
            x_pix_start = -self.view_offset_x
        else:
            # Map > View - determine layout in X direction
            start_x_tile = int(self.view_offset_x / self.tile_size_x)
            stop_x_tile = int((self.view_offset_x + self.view_width
                               + self.tile_size_x - 1) / self.tile_size_x)
            stop_x_tile = min(self.tiles.num_tiles_x-1, stop_x_tile) + 1
            col_list = range(start_x_tile, stop_x_tile)
            x_pix_start = start_x_tile * self.tile_size_y - self.view_offset_x

        if self.view_offset_y < 0:
            # View > Map in Y - centre in Y direction
            row_list = range(self.tiles.num_tiles_y)
            y_pix_start = -self.view_offset_y
        else:
            # Map > View - determine layout in Y direction
            start_y_tile = int(self.view_offset_y / self.tile_size_y)
            stop_y_tile = int((self.view_offset_y + self.view_height
                               + self.tile_size_y - 1) / self.tile_size_y)
            stop_y_tile = min(self.tiles.num_tiles_y-1, stop_y_tile) + 1
            row_list = range(start_y_tile, stop_y_tile)
            y_pix_start = start_y_tile * self.tile_size_y - self.view_offset_y

        # start pasting tiles onto the view
        # use x_pix and y_pix to place tiles
        x_pix = x_pix_start
        for x in col_list:
            y_pix = y_pix_start
            for y in row_list:
                tile = self.tiles.GetTile(x, y)
                dc.DrawBitmap(tile, x_pix, y_pix, False)
                y_pix += self.tile_size_y
            x_pix += self.tile_size_x

        # draw layers
        for id in self.layer_z_order:
            l = self.layer_mapping[id]
            if l.visible and self.level in l.show_levels:
                l.painter(dc, l.data, map_rel=l.map_rel)

        # draw selection rectangle, if any
        if self.sbox_1_x:
            penclr = wx.Colour(0, 0, 255)
            pen = wx.Pen(penclr, 1, wx.USER_DASH)
            pen.SetDashes([1, 1, 1, 1])
            dc.SetPen(pen)
            brushclr = wx.Colour(255, 255, 255)
            dc.SetBrush(wx.Brush(brushclr, style=wx.TRANSPARENT))
            dc.DrawRectangle(self.sbox_1_x, self.sbox_1_y,
                             self.sbox_w, self.sbox_h)

    ######
    # Miscellaneous
    ######

    def View2Geo(self, view):
        """Convert a view coords position to a geo coords position.

        view  tuple of view coords (xview, yview)

        Returns a tuple of geo coords (xgeo, ygeo);
        """

        (xview, yview) = view
        xtile = float(self.view_offset_x + xview) / self.tile_size_x
        ytile = float(self.view_offset_y + yview) / self.tile_size_y

        return self.tiles.Tile2Geo((xtile, ytile))

    def ResizeCallback(self, event=None):
        """Handle a window resize.

        event  that caused the resize, may be None (not used)

        Handle all possible states of view and map:
           . new view entirely within map
           . map smaller than view (just centre map)

        Set up view state.
        """

        # get new size of the view
        (self.view_width, self.view_height) = self.GetClientSizeTuple()
        self.max_x_offset = self.map_width - self.view_width
        self.max_y_offset = self.map_height - self.view_height

        # if map > view in X axis
        if self.map_width > self.view_width:
            # do nothing unless background is showing
            # if map left edge right of view edge
            if self.view_offset_x < 0:
                # move view to hide background at left
                self.view_offset_x = 0
            elif self.view_offset_x + self.view_width > self.map_width:
                # move view to hide background at right
                self.view_offset_x = self.map_width - self.view_width
        else:
            # else view >= map - centre map in X direction
            self.view_offset_x = self.max_x_offset / 2

        # if map > view in Y axis
        if self.map_height > self.view_height:
            # do nothing unless background is showing
            # if map top edge below view edge
            if self.view_offset_y < 0:
                # move view to hide background at top
                self.view_offset_y = 0
            elif self.view_offset_y + self.view_height > self.map_height:
                # move view to hide background at bottom
                self.view_offset_y = self.map_height - self.view_height
        else:
            # else view >= map - centre map in Y direction
            self.view_offset_y = self.max_y_offset / 2

        # set the left/right/top/bottom lon/lat extents
        self.RecalcViewLimits()

    def RecalcViewLimits(self):
        """Recalculate the view geo extent values.

        Assumes only:
            self.view_offset_x
            self.view_offset_y
            self.tiles.tile_size_x
            self.tiles.tile_size_y
        values have been set.  All are map pixel values.
        """

        # get geo coords of top-left of view
        tltile_x = float(self.view_offset_x) / self.tiles.tile_size_x
        tltile_y = float(self.view_offset_y) / self.tiles.tile_size_y
        (self.view_llon, self.view_tlat) = self.tiles.Tile2Geo((tltile_x,
                                                                tltile_y))

        # then get geo coords of bottom-right of view
        tltile_x = (float(self.view_offset_x + self.view_width)
                        / self.tiles.tile_size_x)
        tltile_y = (float(self.view_offset_y + self.view_height)
                        / self.tiles.tile_size_y)
        (self.view_rlon, self.view_blat) = self.tiles.Tile2Geo((tltile_x,
                                                                tltile_y))

    def ZoomToLevel(self, level):
        """Use a new tile level.

        level  the new tile level to use.

        Returns True if all went well.
        """

        if self.min_level <= level <= self.max_level:
            self.tiles.UseLevel(level)
            self.level = level
            self.map_width = self.tiles.num_tiles_x * self.tiles.tile_size_x
            self.map_height = self.tiles.num_tiles_y * self.tiles.tile_size_y
            (self.map_llon, self.map_rlon,
                    self.map_blat, self.map_tlat) = self.tiles.extent

            # to set some state variables
            self.OnSize()

            # raise level change event
            self.RaiseEventLevel(level)

            return True

        return False

    ######
    # Select helpers - get objects that were selected
    ######

    def GetPointInLayer(self, layer, pt):
        """Determine if clicked location selects a point in layer data.

        layer  layer object we are looking in
        pt     click location tuple (geo or view coordinates)

        We must look for the nearest point to the click.

        Return None (no selection) or (point, data, None) of selected point
        where point is [(x,y,attrib)] where X and Y are map or view relative
        depending on layer.map_rel.  'data' is the data object associated with
        each selected point.  The None is a placeholder for the relative
        selection point, which is meaningless for point selection.
        """

# TODO: speed this up?  Do we need to??
# http://en.wikipedia.org/wiki/Kd-tree
# would need to create kd-tree in AddLayer()

        result = None
        delta = layer.delta
        dist = 9999999.0        # more than possible

        # get correct pex function and click point in correct coords
        pex = self.PexPointView
        clickpt = pt
        if layer.map_rel:
            pex = self.PexPoint
            clickpt = self.Geo2View(pt)

        # get selected point on map/view
        (xclick, yclick) = clickpt
        for (x, y, place, radius, colour, x_off, y_off, udata) in layer.data:
            (vp, _) = pex(place, (x,y), x_off, y_off, radius)
            if vp:
                (vx, vy) = vp
                d = (vx - xclick)*(vx - xclick) + (vy - yclick)*(vy - yclick)
                if d < dist:
                    rpt = (x, y, {'placement': place,
                                  'radius': radius,
                                  'colour': colour,
                                  'offset_x': x_off,
                                  'offset_y': y_off})
                    result = ([rpt], udata, None)
                    dist = d

        if dist <= layer.delta:
            return result
        return None

    def GetBoxSelPointsInLayer(self, layer, ll, ur):
        """Get list of points inside box.

        layer  reference to layer object we are working on
        ll     lower-left corner point of selection box (geo or view)
        ur     upper-right corner point of selection box (geo or view)

        Return a tuple (selection, data) where 'selection' is a list of
        selected point positions (xgeo,ygeo) and 'data' is a list of userdata
        objects associated withe selected points.

        If nothing is selected return None.
        """

        # get a list of points inside the selection box
        selection = []
        data = []

        # get correct pex function and box limits in view coords
        pex = self.PexPointView
        (blx, bby) = ll
        (brx, bty) = ur
        if layer.map_rel:
            pex = self.PexPoint
            (blx, bby) = self.Geo2View(ll)
            (brx, bty) = self.Geo2View(ur)

        # get points selection
        for (x, y, place, radius, colour, x_off, y_off, udata) in layer.data:
            (vp, _) = pex(place, (x,y), x_off, y_off, radius)
            if vp:
                (vpx, vpy) = vp
                if blx <= vpx <= brx and bby >= vpy >= bty:
                    selection.append((x, y, {'placement': place,
                                             'radius': radius,
                                             'colour': colour,
                                             'offset_x': x_off,
                                             'offset_y': y_off}))
                    data.append(udata)

        if selection:
            return (selection, data, None)
        return None

    def GetImageInLayer(self, layer, point):
        """Decide if click location selects image object(s) in layer data.

        layer  layer object we are looking in
        point  click location tuple (geo or view)

        Returns either None if no selection or a tuple (selection, data, relsel)
        where 'selection' is a tuple (xgeo,ygeo) or (xview,yview) of the object
        placement point, 'data' is the data object associated with the selected
        object and 'relsel' is the relative position within the selected object
        of the mouse click.

        Note that there could conceivably be more than one image selectable in
        the layer at the mouse click position but only the first is selected.
        """

        (ptx, pty) = point
        result = None

        # get correct pex function and click point into view coords
        clickpt = point
        pex = self.PexExtentView
        if layer.map_rel:
            clickpt = self.Geo2View(point)
            pex = self.PexExtent
        (xclick, yclick) = clickpt

        # select image
        for (x, y, bmp, w, h, place,
                x_off, y_off, radius, colour, udata) in layer.data:
            (_, e) = pex(place, (x,y), x_off, y_off, w, h)
            if e:
                (lx, rx, ty, by) = e
                if lx <= xclick <= rx and ty <= yclick <= by:
                    selection = [(x, y, bmp, {'placement': place,
                                              'radius': radius,
                                              'colour': colour,
                                              'offset_x': x_off,
                                              'offset_y': y_off})]
                    relsel = (int(xclick - lx), int(yclick - ty))
                    result = (selection, udata, relsel)
                    break

        return result

    def GetBoxSelImagesInLayer(self, layer, ll, ur):
        """Get list of images inside selection box.

        layer  reference to layer object we are working on
        ll     lower-left corner point of selection box (geo or view coords)
        ur     upper-right corner point of selection box (geo or view coords)

        Return a tuple (selection, data) where 'selection' is a list of
        selected point positions (xgeo,ygeo) and 'data' is a list of userdata
        objects associated withe selected points.

        If nothing is selected return None.
        """

        # get correct pex function and box limits in view coords
        pex = self.PexExtentView
        if layer.map_rel:
            pex = self.PexExtent
            ll = self.Geo2View(ll)
            ur = self.Geo2View(ur)
        (vboxlx, vboxby) = ll
        (vboxrx, vboxty) = ur

        # select images in map/view
        selection = []
        data = []
        for (x, y, bmp, w, h, place,
                x_off, y_off, radius, colour, udata) in layer.data:
            (_, e) = pex(place, (x,y), x_off, y_off, w, h)
            if e:
                (li, ri, ti, bi) = e    # image extents (view coords)
                if (vboxlx <= li and ri <= vboxrx
                        and vboxty <= ti and bi <= vboxby):
                    selection.append((x, y, bmp, {'placement': place,
                                                  'radius': radius,
                                                  'colour': colour,
                                                  'offset_x': x_off,
                                                  'offset_y': y_off}))
                    data.append(udata)

        if not selection:
            return None
        return (selection, data, None)

    def GetTextInLayer(self, layer, point):
        """Determine if clicked location selects a text object in layer data.

        layer  layer object we are looking in
        point  click location tuple (view or geo coordinates)

        Return ((x,y), data, None) for the selected text object, or None if
        no selection.  The x and y coordinates are view/geo depending on
        the layer.map_rel value.

        ONLY SELECTS ON POINT, NOT EXTENT.
        """

        result = None
        delta = layer.delta
        dist = 9999999.0

        # get correct pex function and mouse click in view coords
        pex = self.PexPointView
        clickpt = point
        if layer.map_rel:
            pex = self.PexPoint
            clickpt = self.Geo2View(point)
        (xclick, yclick) = clickpt

        # select text in map/view layer
        for (x, y, text, place, radius, colour,                                                                                                              
                 tcolour, fname, fsize, x_off, y_off, data) in layer.data:
            (vp, ex) = pex(place, (x,y), 0, 0, radius)
            if vp:
                (px, py) = vp
                d = (px - xclick)**2 + (py - yclick)**2
                if d < dist:
                    selection = (x, y, text, {'placement': place,
                                              'radius': radius,
                                              'colour': colour,
                                              'textcolour': tcolour,
                                              'fontname': fname,
                                              'fontsize': fsize,
                                              'offset_x': x_off,
                                              'offset_y': y_off})
                    result = ([selection], data, None)
                    dist = d

        if dist <= delta:
            return result
        return None

    def GetBoxSelTextsInLayer(self, layer, ll, ur):
        """Get list of text objects inside box ll-ur.

        layer  reference to layer object we are working on
        ll     lower-left corner point of selection box (geo or view)
        ur     upper-right corner point of selection box (geo or view)

        The 'll' and 'ur' points are in view or geo coords, depending on
        the layer.map_rel value.

        Returns (selection, data, None) where 'selection' is a list of text
        positions (geo or view, depending on layer.map_rel) and 'data' is a list
        of userdata objects associated with the selected text objects.

        Returns None if no selection.

        ONLY SELECTS ON POINT, NOT EXTENT.
        """

        selection = []
        data = []

        # get correct pex function and box limits in view coords
        pex = self.PexPointView
        if layer.map_rel:
            pex = self.PexPoint
            ll = self.Geo2View(ll)
            ur = self.Geo2View(ur)
        (lx, by) = ll                                                                                                                                        
        (rx, ty) = ur

        # get texts inside box
        for (x, y, text, place, radius, colour,
                tcolour, fname, fsize, x_off, y_off, udata) in layer.data:
            (vp, ex) = pex(place, (x,y), x_off, y_off, radius)
            if vp:
                (px, py) = vp
                if lx <= px <= rx and ty <= py <= by:
                    sel = (x, y, text, {'placement': place,
                                        'radius': radius,
                                        'colour': colour,
                                        'textcolour': tcolour,
                                        'fontname': fname,
                                        'fontsize': fsize,
                                        'offset_x': x_off,
                                        'offset_y': y_off})
                    selection.append(sel)
                    data.append(udata)

        if selection:
            return (selection, data, None)
        return None

    def GetPolygonInLayer(self, layer, point):
        """Get first polygon object clicked in layer data.

        layer  layer object we are looking in
        point  tuple of click position (xgeo,ygeo) or (xview,yview)

        Returns an iterable: ((x,y), udata) of the first polygon selected.
        Returns None if no polygon selected.
        """

        result = None

        # get correct 'point in polygon' routine
        pip = self.point_in_poly_view
        if layer.map_rel:
            pip = self.point_in_poly_geo

        # check polyons in layer, choose first point is inside
        for (poly, place, width, colour, close,
                 filled, fcolour, x_off, y_off, udata)in layer.data:
            if pip(poly, point, place, x_off, y_off):
                sel = (poly, {'placement': place,
                              'offset_x': x_off,
                              'offset_y': y_off})
                result = ([sel], udata, None)
                break

        return result

    def GetBoxSelPolygonsInLayer(self, layer, p1, p2):
        """Get list of polygons inside box p1-p2 in given layer.

        layer  reference to layer object we are working on
        p1     bottom-left corner point of selection box (geo or view)
        p2     top-right corner point of selection box (geo or view)

        Return a tuple (selection, data, None) where 'selection' is a list of
        iterables of vertex positions and 'data' is  list of data objects
        associated with each polygon selected.
        """

        selection = []
        data = []

        # get correct pex function and box limits in view coords
        pex = self.PexPolyView
        if layer.map_rel:
            pex = self.PexPoly
            p1 = self.Geo2View(p1)
            p2 = self.Geo2View(p2)
        (lx, by) = p1
        (rx, ty) = p2

        # check polygons in layer
        for (poly, place, width, colour, close,
                filled, fcolour, x_off, y_off, udata) in layer.data:
            (pt, ex) = pex(place, poly, x_off, y_off)
            if ex:
                (plx, prx, pty, pby) = ex
                if lx <= plx and prx <= rx and ty <= pty and pby <= by:
                    sel = (poly, {'placement': place,
                                  'offset_x': x_off,
                                  'offset_y': y_off})
                    selection.append(sel)
                    data.append(udata)

        if not selection:
            return None
        return (selection, data, None)

    ######
    # The next two routines could be folded into one as they are the same.
    # However, if we ever implement a 'staged' zoom, we need both routines.
    #
    # A 'staged' zoom is something similar to google maps zoom where the
    # existing map image is algorithimically enlarged (or diminished) and
    # is later overwritten with the actual zoomed map tiles.  I think google
    # is using tiles that can be enlarged (diminished) without too much
    # reduction in detail (SVG-ish), but we'll never be doing *that*!
    ######

    def ZoomIn(self, gposn):
        """Zoom map in to the next level.

        gposn  is a tuple (x, y) of geo coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # move to desired position
        self.GotoPosition(gposn)

        # set some internal state through resize code
        self.ResizeCallback()

        # redraw the map
        self.Update()

    def ZoomOut(self, gposn):
        """Zoom map out to the previous level.

        gposn  is a tuple (x, y) of geo coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # move to desired position
        self.GotoPosition(gposn)

        # set some internal state through size code
        self.ResizeCallback()

        # redraw the map
        self.Update()

    ######
    # Routines for pySlip events
    ######

    def SetLevelChangeEvent(self, event):
        """Set event routine on level change.

        event  True if event is to be raised on change
        """

        self.change_level_event = event

    def RaiseEventLevel(self, level):
        """Raise a LEVEL event."""

        if self.change_level_event:
            event = _PySlipEvent(_myEVT_PYSLIP_LEVEL, self.GetId())

            event.type = EventLevel
            event.level = level

            self.GetEventHandler().ProcessEvent(event)

    def SetMousePositionEvent(self, event):
        """Set callback function on mouse move.

        event  True if event is to be raised on mouse move
        """

        self.mouse_position_event = event

    def RaiseEventPosition(self, mposn, vposn):
        """Raise a mouse position event.

        mposn  the new mouse position (in geo coordinates)
        vposn  the new mouse position (in view coordinates)

        Posts a mouse position event with attributes containing the geo and
        view coordinates of the mouse.

        Will raise an event if mouse moves in widget view but mouse cursor
        is NOT on map.  'event.mposn' attribute is None in that case.
        """

        # create event, assume off map
        event = _PySlipEvent(_myEVT_PYSLIP_POSITION, self.GetId())
        event.type = EventPosition
        event.mposn = None
        event.vposn = vposn

        # but if on map, fill in the rest
        if self.mouse_position_event:
            #if mposn and self.PositionIsOnMap(vposn):
            if self.PositionIsOnMap(vposn):
                event.mposn = mposn

        self.GetEventHandler().ProcessEvent(event)

# there is no set_select_event() method and no self.select_event boolean
# flag for the select event as the user controls selectability on a
# layer-by-layer basis.

    def RaiseEventSelect(self, mposn, vposn, layer=None, selection=None,
                         data=None, relsel=None):
        """Raise a point SELECT event.

        mposn      map coordinates of the mouse click
        vposn      view coordinates of the mouse click
        layer      layer the select was on
        selection  None if no selection or a tuple (point, data, relsel) where
                   'point' is the selected object point ((xgeo,ygeo) or
                   (xview,yview)), data is the associated data object and
                   relsel is the relative selection point

        This event is raised even when nothing is selected.  In that case,
        event.layer_id and .selection are None and .mposn and .vposn are the
        mouse click positions.
        """

        log('RaiseEventSelect: mposn=%s, vposn=%s, selection=%s, data=%s, relsel=%s' % (str(mposn), str(vposn), str(selection), str(data), str(relsel)))

        event = _PySlipEvent(_myEVT_PYSLIP_SELECT, self.GetId())

        event.type = EventSelect
        event.mposn = mposn
        event.vposn = vposn
        event.layer_id = layer.id
        event.selection = None
        event.data = None
        event.relsel = None
        if selection:
            (event.selection, event.data, event.relsel) = selection

        self.GetEventHandler().ProcessEvent(event)

    def RaiseEventBoxSelect(self, layer=None, selection=None):
        """Raise a point BOXSELECT event.

        layer      layer the select was on
        selection  a tuple (points, data, relsel) where 'points' is a list of
                   (x,y) tuples, data is a list of associated userdata objects
                   and relsel is None

        This event is raised even when nothing is selected.  In that case,
        event.layer_id, .selection and .data are None.
        """

        event = _PySlipEvent(_myEVT_PYSLIP_BOXSELECT, self.GetId())

        event.type = EventBoxSelect
        event.layer_id = layer.id
        event.selection = None
        event.data = None
        event.relsel = None
        if selection:
            (event.selection, event.data, event.relsel) = selection

        # attributes with no meaning in box select
        event.mposn = None
        event.vposn = None
        event.relsel = None

        self.GetEventHandler().ProcessEvent(event)

    def RaiseEventPolySelect(self, mposn, vposn, layer, selection, data):
        """Raise a polygon SELECT event.

        mposn      map coordinates of the mouse click
        vposn      view coordinates of the mouse click
        layer      layer the select was on
        selection  a list of polygon vertex iterables
        data       a list of polygon data objects
        """

        event = _PySlipEvent(_myEVT_PYSLIP_POLYSELECT, self.GetId())

        event.type = EventPolySelect
        event.mposn = mposn
        event.vposn = vposn
        event.layer_id = layer.id
        event.selection = selection
        event.data = data

        self.GetEventHandler().ProcessEvent(event)

    def RaiseEventPolyBoxSelect(self, layer, selection, data):
        """Raise a polygon BOXSELECT event.

        layer      layer the select was on
        selection  a list of polygon vertex iterables
        data       a list of polygon data objects
        """

        event = _PySlipEvent(_myEVT_PYSLIP_POLYBOXSELECT, self.GetId())

        event.type = EventPolyBoxSelect
        event.layer_id = layer.id
        (event.selection, event.data) = selection

        self.GetEventHandler().ProcessEvent(event)

    ######
    # Various pySlip utility routines
    ######

    @staticmethod
    def point_inside_polygon(point, poly):
        """Decide if point is inside polygon.

        point  tuple of (x,y) coordnates of point in question (geo or view)
        poly   polygon in form [(x1,y1), (x2,y2), ...]

        Returns True if point is properly inside polygon.
        May return True or False if point on edge of polygon.

        Slightly modified version of the 'published' algorithm found on the 'net.
        Instead of indexing into the poly, create a new poly that 'wraps around'.
        Even with the extra code, it runs in 2/3 the time.
        """

        (x, y) = point

        # we want a *copy* of original iterable plus extra wraparound point
        l_poly = list(poly)
        l_poly.append(l_poly[0])  # ensure poly wraps around

        inside = False

        (p1x, p1y) = l_poly[0]

        for (p2x, p2y) in l_poly:
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y-p1y)*(p2x-p1x)/(p2y-p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            (p1x, p1y) = (p2x, p2y)

        return inside

    def point_in_poly_geo(self, poly, geo, placement, offset_x, offset_y):
        """Decide if a point is inside a map-relative polygon.

        poly       an iterable of (x,y) where x,y are in geo coordinates
        geo        tuple (xgeo, ygeo) of point position
        placement  a placement string
        offset_x   X offset in pixels
        offset_y   Y offset in pixels

        The 'geo' point, while in geo coordinates, must be a click point
        within the view.

        Returns True if point is inside the polygon.
        """

        return self.point_inside_polygon(geo, poly)

    def point_in_poly_view(self, poly, view, place, x_off, y_off):
        """Decide if a point is inside a view-relative polygon.

        poly      an iterable of (x,y) where x,y are in view (pixel) coordinates
        ptx       point X coordinate (view)
        pty       point Y coordinate (view)
        place     a placement string
        offset_x  X offset in pixels
        offset_y  Y offset in pixels

        Returns True if point is inside the polygon.
        """

        # convert polygon and placement into list of (x,y) tuples
        view_poly = []
        for (x, y) in poly:
            (x, y) = self.point_placement(place, x, y, x_off, y_off,
                                          self.view_width, self.view_height)
            view_poly.append((x, y))

        # decide if (ptx,pty) is inside polygon
        return self.point_inside_polygon(view, view_poly)

    def GeoExtent(self, geo, place, w, h, x_off, y_off):
        """Get geo extent of area.

        geo           tuple (xgeo, ygeo) of position to place area at
        place         placement string ('cc', 'se', etc)
        w, h          area width and height (pixels)
        x_off, y_off  x and y offset (geo coords)

        Return the geo extent of the area: (llon, rlon, tlat, blat)
        where:
            llon  longitude of left side of area
            rlon  longitude of right side of area
            tlat  top latitude of area
            blat  bottom latitude of area

        If object extent is totally off the map, return None.
        """

        # decide if object CAN be in view
        # check point in lower, right or lower-right quadrants
        (xgeo, ygeo) = geo
        if self.view_rlon < xgeo or self.view_blat > ygeo:
            return None

        # now, figure out point view posn and extent posn from geo coords+
        (vx, vy) = self.Geo2View(geo)
        (tlvx, tlvy) = self.extent_placement(place, vx, vy, x_off, y_off, w, h)
        # tlvx = top-left view X coordinate

        # now get bottom_right corner in pixel coords
        brvx = tlvx + w
        brvy = tlvy + h
        # brvx = bottom-right view X coordinate

        # decide if object is completely on-view
        if (brvx < -w or brvy < -h
                or tlvx > self.view_width or tlvy > self.view_height):
            return None

        # return geo extent
        (llon, tlat) = self.View2Geo((tlvx, tlvy))
        (rlon, blat) = self.View2Geo((brvx, brvy))

        return (llon, rlon, tlat, blat)

    def ViewExtent(self, place, view, w, h, x_off, y_off, dcw=0, dch=0):
        """Get view extent of area.

        place         placement string ('cc', 'se', etc)
        view          tuple (xview,yview) of view coordinates of object point
        w, h          area width and height (pixels)
        x_off, y_off  x and y offset (pixels)

        Return the view extent of the area: (left, right, top, bottom)
        where:
            left    pixel coords of left side of area
            right   pixel coords of right side of area
            top     pixel coords of top of area
            bottom  pixel coords of bottom of area

        Return a tuple (left, right, top, bottom) of the view coordinates of
        the extent rectangle.
        """

        # top left corner
        (x, y) = view
        (left, top) = self.extent_placement(place, x, y, x_off, y_off,
                                            w, h, dcw, dch)

        # bottom right corner
        right = left + w
        bottom = top + h

        return (left, right, top, bottom)

    def PositionIsOnMap(self, posn):
        """Return True if 'posn' is actually on map (not just view).

        posn  a tuple (x,y) position in view pixel coordinates
        """

        if not posn:
            return False

        (x, y) = posn

        if self.view_offset_x < 0:
            if x < -self.view_offset_x:
                return False
            if x > self.view_width + self.view_offset_x:
                return False

        if self.view_offset_y < 0:
            if y < -self.view_offset_y:
                return False
            if y > self.view_height + self.view_offset_y:
                return False

        return True

    def get_i18n_kw(self, kwargs, kws, default):
        """Get alternate international keyword value.

        kwargs   dictionary to look for keyword value
        kws      iterable of keyword spelling strings
        default  default value if no keyword found

        Returns the keyword value.
        """

        result = None
        for kw_str in kws[:-1]:
            result = kwargs.get(kw_str, None)
            if result:
                break
        else:
            result = kwargs.get(kws[-1], default)

        return result

    def info(self, msg):
        """Display an information message, log and graphically."""

        log_msg = '# ' + msg
        length = len(log_msg)
        prefix = '#### Information '
        banner = prefix + '#'*(80 - len(log_msg) - len(prefix))
        log(banner)
        log(log_msg)
        log(banner)

        wx.MessageBox(msg, 'Warning', wx.OK | wx.ICON_INFORMATION)

    def warn(self, msg):
        """Display a warning message, log and graphically."""

        log_msg = '# ' + msg
        length = len(log_msg)
        prefix = '#### Warning '
        banner = prefix + '#'*(80 - len(log_msg) - len(prefix))
        log(banner)
        log(log_msg)
        log(banner)

        wx.MessageBox(msg, 'Warning', wx.OK | wx.ICON_ERROR)

    def sel_box_canonical(self):
        """'Canonicalize' a selection box limits.

        Uses instance variables (all in view coordinates):
            self.sbox_1_x    X position of box select start
            self.sbox_1_y    Y position of box select start
            self.sbox_w      width of selection box (start to finish)
            self.sbox_h      height of selection box (start to finish)

        Four ways to draw the selection box (starting in each of the four
        corners), so four cases.

        The sign of the width/height values are decided with respect to the
        origin at view top-left corner.  That is, a negative width means
        the box was started at the right and swept to the left.  A negative
        height means the selection started low and swept high in the view.

        Returns a tuple (llx, llr, urx, ury) where llx is lower left X, ury is
        upper right corner Y, etc.  All returned values in view coordinates.
        """

        if self.sbox_h >= 0:
            if self.sbox_w >= 0:
                # 2
                ll_corner_vx = self.sbox_1_x
                ll_corner_vy = self.sbox_1_y + self.sbox_h
                tr_corner_vx = self.sbox_1_x + self.sbox_w
                tr_corner_vy = self.sbox_1_y
            else:
                # 1
                ll_corner_vx = self.sbox_1_x + self.sbox_w
                ll_corner_vy = self.sbox_1_y + self.sbox_h
                tr_corner_vx = self.sbox_1_x
                tr_corner_vy = self.sbox_1_y
        else:
            if self.sbox_w >= 0:
                # 3
                ll_corner_vx = self.sbox_1_x
                ll_corner_vy = self.sbox_1_y
                tr_corner_vx = self.sbox_1_x + self.sbox_w
                tr_corner_vy = self.sbox_1_y + self.sbox_h
            else:
                # 4
                ll_corner_vx = self.sbox_1_x + self.sbox_w
                ll_corner_vy = self.sbox_1_y
                tr_corner_vx = self.sbox_1_x
                tr_corner_vy = self.sbox_1_y + self.sbox_h

        return (ll_corner_vx, ll_corner_vy, tr_corner_vx, tr_corner_vy)

######
# Placement routines instead of original 'exec' code.
# Code in test_assumptions.py shows this is faster.
######

    @staticmethod
    def point_placement(place, x, y, x_off, y_off, dcw=0, dch=0):
        """Perform map- or view-relative placement for a single point.

        place         placement key string
        x, y          point relative to placement origin
        x_off, y_off  offset from point
        dcw, dch      width, height of the view draw context (0 if map-rel)

        Returns a tuple (x, y).
        """

        dcw2 = dcw/2
        dch2 = dch/2

        if place == 'cc':   x+=dcw2;       y+=dch2
        elif place == 'nw': x+=x_off;      y+=y_off
        elif place == 'cn': x+=dcw2;       y+=y_off
        elif place == 'ne': x+=dcw-x_off;  y+=y_off
        elif place == 'ce': x+=dcw-x_off;  y+=dch2
        elif place == 'se': x+=dcw-x_off;  y+=dch-y_off
        elif place == 'cs': x+=dcw2;       y+=dch-y_off
        elif place == 'sw': x+=x_off;      y+=dch-y_off
        elif place == 'cw': x+=x_off;      y+=dch2

        return (x, y)

    @staticmethod
    def extent_placement(place, x, y, x_off, y_off, w, h, dcw=0, dch=0):
        """Perform map- and view-relative placement for an extent object.

        place         placement key string
        x, y          point relative to placement origin
        x_off, y_off  offset from point
        w, h          width, height of the image
        dcw, dcw      width/height of the view draw context

        Returns a tuple (x, y).
        """

        w2 = w/2
        h2 = h/2

        dcw2 = dcw/2
        dch2 = dch/2

        if place == 'cc':   x+=dcw2-w2;       y+=dch2-h2
        elif place == 'nw': x+=x_off;         y+=y_off
        elif place == 'cn': x+=dcw2-w2;       y+=y_off
        elif place == 'ne': x+=dcw-w-x_off;   y+=y_off
        elif place == 'ce': x+=dcw-w-x_off;   y+=dch2-h2
        elif place == 'se': x+=dcw-w-x_off;   y+=dch-h-y_off
        elif place == 'cs': x+=dcw2-w2;       y+=dch-h-y_off
        elif place == 'sw': x+=x_off;         y+=dch-h-y_off
        elif place == 'cw': x+=x_off;         y+=dch2-h2

        return (x, y)

