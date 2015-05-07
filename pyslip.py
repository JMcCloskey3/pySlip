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
from PIL import Image
import wx

# if we don't have log.py, don't crash
try:
    import log
    log = log.Log('pyslip.log', log.Log.DEBUG)
except ImportError:
    def log(*args, **kwargs):
        pass


__version__ = '3.0'

__all__ = ['PySlip']

# type of SELECT events
(EventLevel, EventPosition, EventSelect, EventBoxSelect,
    EventPolySelect, EventPolyBoxSelect, EventRightSelect) = range(7)


######
# utility routines.
######

def point_inside_polygon(x, y, poly):
    """Decide if point is inside polygon.

    x     x coord of point in question
    y     y coord of point in question
    poly  polygon in form [(x1,y1), (x2,y2), ...]

    Returns True if point is properly inside polygon.
    May return True or False if point on edge of polygon.

    Slightly modified version of the 'published' algorithm found on the 'net.
    Instead of indexing into the poly, create a new poly that 'wraps around'.
    Even with the extra code, it runs in 2/3 the time.
    """

    # we want a *copy* of original iterable plus extra wraparound point
#FASTER
#    l_poly = list(poly)
    l_poly = poly[:]
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

    DefaultDelta = 500      # default selection delta

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
        self.data = data                # data that defined the layer
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

    # keep a temporary list of placement dictionaries for later compilation
    placements = []

    # list of valid placement values
    valid_placements = ['cc', 'nw', 'cn', 'ne', 'ce',
                        'se', 'cs', 'sw', 'cw', None, False, '']

    # dictionary for map-relative point placement
    # assumes variables x, y, x_off, y_off are set
    # perturbs x and y to point centre for drawing
    point_map_placement = {'cc': '',
                           'nw': 'x+=x_off;   y+=y_off',
                           'cn': 'pass;       y+=y_off',
                           'ne': 'x-=x_off;   y+=y_off',
                           'ce': 'x-=x_off',
                           'se': 'x-=x_off;   y-=y_off',
                           'cs': 'pass;       y-=y_off',
                           'sw': 'x+=x_off;   y-=y_off',
                           'cw': 'x+=x_off;',
                           None: '',
                           False: '',
                           '':   ''}
    placements.append(point_map_placement)

    # dictionary for view-relative point placement
    # assumes variables x, y, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to point centre for drawing
    point_view_placement = {'cc': 'x+=dc_w2;      y+=dc_h2',
                            'nw': 'x+=x_off;      y+=y_off',
                            'cn': 'x+=dc_w2;      y+=y_off',
                            'ne': 'x+=dc_w-x_off; y+=y_off',
                            'ce': 'x+=dc_w-x_off; y+=dc_h2',
                            'se': 'x+=dc_w-x_off; y+=dc_h-y_off',
                            'cs': 'x+=dc_w2;      y+=dc_h-y_off',
                            'sw': 'x+=x_off;      y+=dc_h-y_off',
                            'cw': 'x+=x_off;      y+=dc_h2',
                            None: '',
                            False: '',
                            '':   ''}
    placements.append(point_view_placement)

    # dictionary for map-relative image placement
    # assumes variables x, y, w, h, w2, h2, x_off & y_off are set
    # perturbs x and y to top-left image corner for placing
    # all values are view pixel coordinates
    image_map_placement = {'cc': 'x+=x_off-w2;  y+=y_off-h2',
                           'nw': 'x+=x_off;     y+=y_off',
                           'cn': 'x+=x_off-w2;  y+=y_off',
                           'ne': 'x+=x_off-w;   y+=y_off',
                           'ce': 'x+=x_off-w;   y+=y_off-h2',
                           'se': 'x+=x_off-w;   y+=y_off-h',
                           'cs': 'x+=x_off-w2;  y+=y_off-h',
                           'sw': 'x+=x_off;     y+=y_off-h',
                           'cw': 'x+=x_off;     y+=y_off-h2',
                           None: '',
                           False: '',
                           '': ''}
    placements.append(image_map_placement)

    # dictionary for view-relative image placement
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to top-left image corner for drawing
    image_view_placement = {'cc': 'x+=dc_w2-w2;     y+=dc_h2-h2',
                            'nw': 'x+=x_off;        y+=y_off',
                            'cn': 'x+=dc_w2-w2;     y+=y_off',
                            'ne': 'x+=dc_w-w-x_off; y+=y_off',
                            'ce': 'x+=dc_w-w-x_off; y+=dc_h2-h2',
                            'se': 'x+=dc_w-w-x_off; y+=dc_h-h-y_off',
                            'cs': 'x+=dc_w2-w2;     y+=dc_h-h-y_off',
                            'sw': 'x+=x_off;        y+=dc_h-h-y_off',
                            'cw': 'x+=x_off;        y+=dc_h2-h2',
                            None: '',
                            False: '',
                            '': ''}
    placements.append(image_view_placement)

    # map-relative text placement dictionary
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # w and h are text width and height
    # perturbs x and y to correct values for the placement
    text_map_placement = {'cc': 'x+=-w2;      y+=-h2',
                          'nw': 'x+=x_off;    y+=y_off',
                          'cn': 'x+=-w2;      y+=y_off',
                          'ne': 'x+=-w-x_off; y+=y_off',
                          'ce': 'x+=-w-x_off; y+=-h2',
                          'se': 'x+=-w-x_off; y+=-h-y_off',
                          'cs': 'x+=-w2;      y+=-h-y_off',
                          'sw': 'x+=x_off;    y+=-h-y_off',
                          'cw': 'x+=x_off;    y+=-h2',
                          None: '',
                          False: '',
                          '': ''}
    placements.append(text_map_placement)

    # view-relative text placement dictionary
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # w and h are text width and height
    # perturbs x and y to correct values for the placement
    text_view_placement = {'cc': 'x+=dc_w2-w2;      y+=dc_h2-h2',
                           'nw': 'x+=x_off;         y+=y_off',
                           'cn': 'x+=dc_w2-w2;      y+=y_off',
                           'ne': 'x+=dc_w-w-x_off;  y+=y_off',
                           'ce': 'x+=dc_w-w-x_off;  y+=dc_h2-h2',
                           'se': 'x+=dc_w-w-x_off;  y+=dc_h-h-y_off',
                           'cs': 'x+=dc_w2-w2;      y+=dc_h-h-y_off',
                           'sw': 'x+=x_off;         y+=dc_h-h-y_off',
                           'cw': 'x+=x_off;         y+=dc_h2-h2',
                           None: '',
                           False: '',
                           '': ''}
    placements.append(text_view_placement)

    # dictionary for map-relative polygon placement
    # assumes variables x, y, x_off and y_off are set
    # perturbs x and y to point centre for drawing
    poly_map_placement = {'cc': '',
                          'nw': 'x+=x_off;  y+=y_off',
                          'cn': 'pass;      y+=y_off',
                          'ne': 'x-=x_off;  y+=y_off',
                          'ce': 'x-=x_off',
                          'se': 'x-=x_off;  y-=y_off',
                          'cs': 'pass;      y-=y_off',
                          'sw': 'x+=x_off;  y-=y_off',
                          'cw': 'x+=x_off',
                          None: '',
                          False: '',
                          '': ''}
    placements.append(poly_map_placement)

    # view-relative polygon placement dictionary
    # assumes variables x, y, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to correct values for the placement
    poly_view_placement = {'cc': 'x+=dc_w2;      y+=dc_h2',
                           'nw': 'x+=x_off;      y+=y_off',
                           'cn': 'x+=dc_w2;      y+=y_off',
                           'ne': 'x+=dc_w-x_off; y+=y_off',
                           'ce': 'x+=dc_w-x_off; y+=dc_h2',
                           'se': 'x+=dc_w-x_off; y+=dc_h-y_off',
                           'cs': 'x+=dc_w2;      y+=dc_h-y_off',
                           'sw': 'x+=x_off;      y+=dc_h-y_off',
                           'cw': 'x+=x_off;      y+=dc_h2',
                            None: '',
                            False: '',
                           '': ''}
    placements.append(poly_view_placement)

    # how a single point in an image is perturbed in a map
    # this is used by GeoExtent()
    point_map_perturb = {'cc': 'x+=x_off-w2;      y+=y_off-h2',
                         'nw': 'x+=x_off;         y+=y_off',
                         'cn': 'x+=x_off-w2;      y+=y_off',
                         'ne': 'x+=x_off-w;       y+=y_off',
                         'ce': 'x+=x_off-w;       y+=y_off-h2',
                         'se': 'x+=x_off-w;       y+=y_off-h',
                         'cs': 'x+=x_off-w2;      y+=y_off-h',
                         'sw': 'x+=x_off;         y+=y_off-h',
                         'cw': 'x+=x_off;         y+=y_off-h2',
                          None: '',
                          False: '',
                          '':   '',
                         }
    placements.append(point_map_perturb)

    # how a single point in an image is perturbed in a view
    # this is used by ViewExtent()
    point_view_perturb = {'cc': 'x+=dc_w2-w2;      y+=dc_h2-h2',
                          'nw': 'x+=x_off;         y+=y_off',
                          'cn': 'x+=dc_w2-w2;      y+=y_off',
                          'ne': 'x+=dc_w-x_off-w;  y+=y_off',
                          'ce': 'x+=dc_w-x_off-w;  y+=dc_h2-h2',
                          'se': 'x+=dc_w-x_off-w;  y+=dc_h-y_off-h',
                          'cs': 'x+=dc_w2-w2;      y+=dc_h-y_off-h',
                          'sw': 'x+=x_off;         y+=dc_h-y_off-h',
                          'cw': 'x+=x_off;         y+=dc_h2-h2',
                          None: '',
                          False: '',
                          '':   '',
                         }
    placements.append(point_view_perturb)

    # now pre-compile all the dictionary placement strings
    for p_dict in placements:
        for key in p_dict:
            p_dict[key] = compile(p_dict[key], 'string', 'exec')
    del placements

    # panel background colour
    BackgroundColour = '#808080'

    # default point attributes - map relative
    DefaultPointPlacement = ''
    DefaultPointRadius = 3
    DefaultPointColour = wx.RED
    DefaultPointOffsetX = 0
    DefaultPointOffsetY = 0
    DefaultPointData = None

    # default point attributes - view relative
    DefaultPointViewPlacement = ''
    DefaultPointViewRadius = 3
    DefaultPointViewColour = wx.RED
    DefaultPointViewOffsetX = 0
    DefaultPointViewOffsetY = 0
    DefaultPointViewData = None

    # default image attributes - map relative
    DefaultImagePlacement = ''
    DefaultImageRadius = 0
    DefaultImageColour = wx.BLACK
    DefaultImageOffsetX = 0
    DefaultImageOffsetY = 0
    DefaultImageData = None

    # default image attributes - view relative
    DefaultImageViewPlacement = ''
    DefaultImageViewRadius = 0
    DefaultImageViewColour = wx.BLACK
    DefaultImageViewOffsetX = 0
    DefaultImageViewOffsetY = 0
    DefaultImageViewData = None

    # default text attributes - map relative
    DefaultTextPlacement = ''
    DefaultTextRadius = 2
    DefaultTextColour = wx.BLACK
    DefaultTextTextColour = wx.BLACK
    DefaultTextOffsetX = 5
    DefaultTextOffsetY = 1
    DefaultTextFontname = 'Arial'
    DefaultTextFontSize = 10
    DefaultTextData = None

    # default text attributes - view relative
    DefaultTextViewPlacement = ''
    DefaultTextViewRadius = 0
    DefaultTextViewColour = wx.BLACK
    DefaultTextViewTextColour = wx.BLACK
    DefaultTextViewOffsetX = 0
    DefaultTextViewOffsetY = 0
    DefaultTextViewFontname = 'Arial'
    DefaultTextViewFontSize = 10
    DefaultTextViewData = None

    # default polygon attributes - map view
    DefaultPolyPlacement = ''
    DefaultPolyWidth = 1
    DefaultPolyColour = wx.RED
    DefaultPolyClose = False
    DefaultPolyFilled = False
    DefaultPolyFillcolour = 'blue'
    DefaultPolyOffsetX = 0
    DefaultPolyOffsetY = 0
    DefaultPolyData = None

    # default polygon attributes - view relative
    DefaultPolyViewPlacement = ''
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
#    TypePoint = 0
#    TypeImage = 1
#    TypePoly = 2
#    TypeText = 3


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
        self.move_dx = 0                        # drag delta values
        self.move_dy = 0
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

        # set up dispatch dictionaries for layer select handlers
        # for point select
        self.layerPSelHandler = {self.TypePoint: self.GetNearestPointInLayer,
                                 self.TypeImage: self.GetImageInLayer,
                                 self.TypeText: self.GetNearestTextInLayer,
                                 self.TypePoly: self.GetNearestPolygonInLayer}

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

        log('AddPointLayer: points=%s, map_rel=%s, visible=%s, show_levels=%s'
                % (str(points), str(map_rel), str(visible), str(show_levels)))

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
            log('####: pt=%s' % str(pt))
            if len(pt) == 3:
                (x, y, attributes) = pt
            elif len(pt) == 2:
                (x, y) = pt
                attributes = {}
            else:
                msg = ('Points data must be iterable of tuples: '
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
            if placement not in self.valid_placements:
                msg = ("Points placement valus is invalid, got '%s'"
                       % str(placement))
                raise Exception(msg)

            # append another point to draw data list
            log('===: x=%s, y=%s, radius=%s, offset_x=%s, offset=%s'
                    % (str(x), str(y), str(radius), str(offset_x), str(offset_y)))
            draw_data.append((float(x), float(y), placement.lower(),
                              radius, colour, offset_x, offset_y, udata))

        log('AddPointLayer: draw_data=%s' % str(draw_data))
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
                         offset_x   hotspot X offset in pixels
                         offset_y   hotspot Y offset in pixels
                         data       image user data

        The hotspot is placed at (lon, lat) or (x, y).  'placement' controls
        where the image is displayed relative to the hotspot.
        """

        # merge global and layer defaults
        if map_rel:
            default_placement = kwargs.get('placement', self.DefaultImagePlacement)
            default_offset_x = kwargs.get('offset_x', self.DefaultImageOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultImageOffsetY)
            default_data = kwargs.get('data', self.DefaultImageData)
        else:
            default_placement = kwargs.get('placement', self.DefaultImageViewPlacement)
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
                msg = ('Points data must be iterable of tuples: '
                       '(x, y, fname[, dict])\nGot: %s' % str(d))
                raise Exception(msg)

            # get image specific values, if any
            placement = attributes.get('placement', default_placement)
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

            log('AddImageLayer: lon=%s, lat=%s, w=%s, h=%s, offset_x=%s, offset_y=%s'
                    % (str(lon), str(lat), str(w), str(h), str(offset_x), str(offset_y)))
            draw_data.append((float(lon), float(lat), bmap, w, h,
                              placement.lower(), offset_x, offset_y, udata))

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
            log('AddTextLayer: lon=%s, lat=%s, radius=%s, offset_x=%s, offset_y=%s'
                    % (str(lon), str(lat), str(radius), str(offset_x), str(offset_y)))
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
#FASTER
                p = list(p)     # must get a *copy*
                p.append(p[0])
#                new_p = p[:]
#                p = new_p
#                p.append(p[0])

            draw_data.append((p, placement.lower(), width, colour, close,
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

        log('AddLayer: data=%s' % str(data))

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

        # just in case we got None
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

        # draw points on map/view
        if map_rel:
            for (lon, lat, place,
                 radius, colour, x_off, y_off, udata) in data:
                pt = self.ConvertGeo2ViewMasked(lon, lat)
                if pt and radius:
                    dc.SetPen(wx.Pen(colour))
                    dc.SetBrush(wx.Brush(colour))
                    (x, y) = pt
                    exec self.point_map_placement[place]
                    dc.DrawCircle(x, y, radius)
        else:   # view
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_h -= 1
            dc_w -= 1
            for (x, y, place, radius, colour, x_off, y_off, udata) in data:
                if radius:
                    dc.SetPen(wx.Pen(colour))
                    dc.SetBrush(wx.Brush(colour))
                    exec self.point_view_placement[place]
                    dc.DrawCircle(x, y, radius)

    def DrawImageLayer(self, dc, images, map_rel):
        """Draw an image Layer on the view.

        dc       the device context to draw on
        images   a sequence of image tuple sequences
                   (x,y,bmap,w,h,placement,offset_x,offset_y,idata)
        map_rel  points relative to map if True, else relative to view
        """

        # allow transparent colours
        dc = wx.GCDC(dc)

        # draw images
        if map_rel:
            # draw images on the map
            for (lon, lat, bmap, w, h, place, x_off, y_off, idata) in images:
                w2 = w / 2
                h2 = h / 2
                pt = self.ConvertGeo2ViewMasked(lon, lat)
                if pt:
                    (x, y) = pt
                    exec self.image_map_placement[place]
                    dc.DrawBitmap(bmap, x, y, False)
        else:
            # draw images on the view
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            for (x, y, bmap, w, h, place, x_off, y_off, idata) in images:
                w2 = w / 2
                h2 = h / 2
                exec self.image_view_placement[place]
                dc.DrawBitmap(bmap, x, y, False)

    def DrawTextLayer(self, dc, text, map_rel):
        """Draw a text Layer on the view.

        dc       the device context to draw on
        text     a sequence of tuples:
                     (lon, lat, tdata, placement, radius, colour, fontname,
                      fontsize, offset_x, offset_y, tdata)
        map_rel  points relative to map if True, else relative to view
        """

        log('DrawTextLayer: text=%s, map_rel=%s' % (text, str(map_rel)))

        if text is None:
            return

        # we need the size of the DC
        dc = wx.GCDC(dc)		# allow transparent colours
        (dc_w, dc_h) = dc.GetSize()

        # draw text on map/view
        if map_rel:
            # draw text on the map
            for t in text:
                (lon, lat, tdata, place, radius, colour, textcolour,
                     fontname, fontsize, x_off, y_off, data) = t

                # convert geo position to view (returns None if off-view)
                pt = self.ConvertGeo2ViewMasked(lon, lat)
                if pt:
                    (x, y) = pt

                    # set font characteristics
                    dc.SetPen(wx.Pen(colour))
                    dc.SetBrush(wx.Brush(colour))
                    dc.SetTextForeground(colour)
                    font = wx.Font(fontsize, wx.SWISS, wx.NORMAL, wx.NORMAL,
                                   False, fontname)
                    dc.SetFont(font)

                    # draw hotpoint circle
                    if radius:
                        dc.DrawCircle(x, y, radius)

                    # place the text relative to hotpoint
                    (w, h, _, _) = dc.GetFullTextExtent(tdata)
                    w2 = w / 2
                    h2 = h / 2
                    exec self.text_map_placement[place]
                    dc.SetTextForeground(textcolour)
                    dc.DrawText(tdata, x, y)
        else:
            # draw text on the view
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_w -= 1
            dc_h -= 1
            for t in text:
                # for each text element, get unpacked data
                (x, y, tdata, place, radius, colour, textcolour,
                     fontname, fontsize, x_off, y_off, data) = t

                # set font characteristics
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                dc.SetTextForeground(colour)
                font = wx.Font(fontsize, wx.SWISS, wx.NORMAL, wx.NORMAL,
                               False, fontname)
                dc.SetFont(font)

                # draw hotpoint - do placement with offsets zero
                if radius:
                    (save_x_off, save_y_off) = (x_off, y_off)   # save offsets
                    (save_x, save_y) = (x, y)                   # and X, Y
                    (w, h, w2, h2, x_off, y_off) = (0, 0, 0, 0, 0, 0)
                    exec self.text_view_placement[place]
                    log('DrawTextLayer: dc.DrawCircle(%s, %s, %s)' % (str(x), str(y), str(radius)))
                    dc.DrawCircle(x, y, radius)
                    (x, y) = (save_x, save_y)                   # restore X, Y
                    (x_off, y_off) = (save_x_off, save_y_off)   # and offsets

                # place the text relative to hotpoint
                (w, h, _, _) = dc.GetFullTextExtent(tdata)  # size of text
                w2 = w / 2
                h2 = h / 2
                exec self.text_view_placement[place]
                dc.SetTextForeground(textcolour)
                dc.DrawText(tdata, x, y)

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

        # draw polygons on map/view
        if map_rel:
            for (p, place, width, colour, closed,
                 filled, fillcolour, x_off, y_off, udata) in data:
                # gather all polygon points as view coords
                pp = []
                for lonlat in p:
                    (lon, lat) = lonlat
                    (tx, ty) = self.tiles.Geo2Tile(lon, lat)
                    x = tx*self.tiles.tile_size_x - self.view_offset_x
                    y = ty*self.tiles.tile_size_y - self.view_offset_y
                    exec self.poly_map_placement[place]
                    pp.append((x, y))

                dc.SetPen(wx.Pen(colour, width=width))

                if filled:
                    dc.SetBrush(wx.Brush(fillcolour))
                else:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)

                if closed:
                    dc.DrawPolygon(pp)
                else:
                    dc.DrawLines(pp)
        else:   # view
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_w -= 1
            dc_h -= 1
            for (p, place, width, colour, closed,
                 filled, fillcolour, x_off, y_off, udata) in data:
                # fetch the exec code, don't refetch for each point in polygon
                place_exec = self.poly_view_placement[place]
                pp = []
                for (x, y) in p:
                    exec place_exec
                    pp.append((x, y))

                dc.SetPen(wx.Pen(colour, width=width))

                if filled:
                    dc.SetBrush(wx.Brush(fillcolour))
                else:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)

                if closed:
                    dc.DrawPolygon(pp)
                else:
                    dc.DrawLines(pp)

    ######
    # Positioning methods
    ######

    def GotoPosition(self, posn):
        """Set view to centre on a position in the current level.

        posn  a tuple (lon,lat) to centre view on

        Sets self.view_offset_x and self.view_offset_y and then calls
        RecalcViewLonLatLimits(), redraws widget.
        """

        (lon, lat) = posn

        # get fractional tile coords of required centre of view
        (xtile, ytile) = self.tiles.Geo2Tile(lon, lat)

        # now calculate view offsets, top, left, bottom and right
        half_width = self.view_width / 2
        centre_pixels_from_map_left = int(xtile * self.tile_size_x)
        self.view_offset_x = centre_pixels_from_map_left - half_width

        half_height = self.view_height / 2
        centre_pixels_from_map_top = int(ytile * self.tile_size_y)
        self.view_offset_y = centre_pixels_from_map_top - half_height

        # set the left/right/top/bottom lon/lat extents and redraw view
        self.RecalcViewLonLatLimits()
        self.Update()

    def GotoLevelAndPosition(self, level, posn):
        """Goto a map level and set view to centre on a position.

        level  the map level to use
        posn   a tuple (lon,lat) to centre view on

        Does nothing if we can't use desired level.
        """

        if self.ZoomToLevel(level):
            self.GotoPosition(posn)

    def ZoomToArea(self, posn, size):
        """Set view to level and position to view an area.

        posn  a tuple (lon,lat) to centre view on
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

        self.GotoLevelAndPosition(level, posn)

    ######
    # Convert between geo and view coordinates
    ######

    def ConvertGeo2ViewMasked(self, lon, lat):
        """Convert a geo (lon+lat) position to view pixel coords.

        lon  longitude of point
        lat  latitude of point

        Return screen pixels coordinates of the point (lon,lat) or None
        if point is off-view.
        """

        if (self.view_llon <= lon <= self.view_rlon and
                self.view_blat <= lat <= self.view_tlat):
            (x, y) = self.tiles.Geo2Tile(lon, lat)
            res_x = (x * self.tiles.tile_size_x) - self.view_offset_x
            res_y = (y * self.tiles.tile_size_y) - self.view_offset_y
            return (res_x, res_y)

        return None

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
        #(xgeo, ygeo) = self.View2Geo(mouse_view)
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

                self.RecalcViewLonLatLimits()

            # redraw client area
            self.Update()

    def OnLeftDown(self, event):
        """Left mouse button down. Prepare for possible drag."""

        click_posn = event.GetPositionTuple()

        if event.ShiftDown():
            self.is_box_select = True
            self.SetCursor(wx.StockCursor(wx.CURSOR_CROSS))
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

        log('OnLeftUp --------------------------------------')

        # turn off any dragging
        self.last_drag_x = self.last_drag_y = None

        # if required, ignore this event
        if self.ignore_next_up:
            self.ignore_next_up = False
            return

        # cursor back to normal
        self.SetCursor(wx.StockCursor(wx.CURSOR_DEFAULT))

        # we need a repaint to remove any selection box, but NOT YET!
        delayed_paint = self.sbox_1_x       # True if box select active

        # if any layers interested, inform of possible select
        if not self.was_dragging:
            if self.is_box_select:
                log('box select')
                # possible box selection
                # get selection box in view coordinates
                ll_corner_vx = self.sbox_1_x
                ll_corner_vy = self.sbox_1_y
                tr_corner_vx = self.sbox_1_x + self.sbox_w
                tr_corner_vy = self.sbox_1_y + self.sbox_h

                # selection box corners in tile coords
                ll_corner_tx = float(ll_corner_vx+self.view_offset_x) / self.tile_size_x
                ll_corner_ty = float(ll_corner_vy+self.view_offset_y) / self.tile_size_y
                tr_corner_tx = float(tr_corner_vx+self.view_offset_x) / self.tile_size_x
                tr_corner_ty = float(tr_corner_vy+self.view_offset_y) / self.tile_size_y

                # selection box in geo coords
                ll_corner_g = self.tiles.Tile2Geo(ll_corner_tx, ll_corner_ty)
                tr_corner_g = self.tiles.Tile2Geo(tr_corner_tx, tr_corner_ty)

                # check each layer for a box select event
                # we work on a copy as user response could change order
                for id in self.layer_z_order[:]:
                    l = self.layer_mapping[id]
                    # if layer visible and selectable
                    if l.selectable and l.visible:
                        if l.map_rel:
                            # map-relative, get all points selected (if any)
                            sel = self.layerBSelHandler[l.type]\
                                      (l, ll_corner_g, tr_corner_g)
                        else:
                            # view-relative
                            sel = self.layerBSelHandler[l.type]\
                                      (l, (ll_corner_vx, ll_corner_vy),
                                      (tr_corner_vx, tr_corner_vy))
                        pts = None
                        data = None
                        if sel:
                            (pts, data) = sel
                        self.RaiseEventBoxSelect(l, pts, data)

                        # user code possibly updated screen
                        delayed_paint = True
                self.is_box_select = False
            else:
                log('point select')
                # possible point selection, get click point in view coords
                clickpt_v = event.GetPositionTuple()
                (clickpt_vx, clickpt_vy) = clickpt_v

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
                        selection = None
                        data= None
                        if sel:
                            (selection, data) = sel
                        self.RaiseEventSelect(mposn=clickpt_g, vposn=clickpt_v,
                                              layer=l,
                                              selection=sel)
                                              #data=data)

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

        # TODO: should ignore double-click off the map, but within view
        # a possible workaround is to limit minimum view level

        # get view coords of mouse double click, want same centre afterwards
        xy = event.GetPositionTuple()

        if event.ShiftDown():
            # zoom out if shift key also down
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut(xy)
        else:
            # zoom in
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn(xy)

    def OnMiddleDown(self, event):
        """Middle mouse button down.  Do nothing in this version."""

        pass

    def OnMiddleUp(self, event):
        """Middle mouse button up.  Do nothing in this version."""

        pass

    def OnRightDown(self, event):
        """Right mouse button down. Prepare for right select (no drag)."""

        click_posn = event.GetPositionTuple()

        if event.ShiftDown():
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
            ll_corner_x = (self.sbox_1_x+self.view_offset_x) / self.tile_size_x
            ll_corner_y = (self.sbox_1_y+self.view_offset_y) / self.tile_size_y

            ll_corner_g = self.tiles.Tile2Geo(ll_corner_x, ll_corner_y)
            tr_corner_g = self.tiles.Tile2Geo(ll_corner_x + self.sbox_w,
                                              ll_corner_y + self.sbox_h)

            # check each layer for a box select event
            # we work on a copy as user response could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                if l.selectable and l.visible:   # and l.event_box_select:
                    if l.map_rel:
                        # map-relative, get all points selected (if any)
                        pts = self.layerBSelHandler[l.type](l, ll_corner_g,
                                                            tr_corner_g)
                    else:
                        # view-relative
                        pts = self.layerBSelHandler[l.type](l,
                                                            (ll_corner_x,
                                                             ll_corner_y),
                                                            (ll_corner_x+self.sbox_w,
                                                             ll_corner_y+self.sbox_h))
                    self.RaiseEventSelect(EventRightBoxSelect, l, pts)

                    # user code possibly updated screen
                    delayed_paint = True
            self.is_box_select = False
        else:
            # possible point selection
            clickpt_v = event.GetPositionTuple()
            (clickpt_v_x, clickpt_v_y) = clickpt_v
            clickpt_v_x = clickpt_v_x + self.view_offset_x
            clickpt_v_y = clickpt_v_y + self.view_offset_y
            clickpt_m = self.tiles.Tile2Geo(clickpt_v_x, clickpt_v_y)

            # check each layer for a point select callback
            # we work on a copy as user callback could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                # if layer visible, selectable and there is a callback
                if l.selectable and l.visible:
                    if l.map_rel:
                        pt = self.layerPSelHandler[l.type](l, clickpt_m)
                    else:
                        pt = self.layerPSelHandler[l.type](l, clickpt_v)
                    self.RaiseEventSelect(EventRightSelect, l, pt,
                                          mposn=clickpt_m,
                                          vposn=clickpt_v)

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

        # determine which way to zoom, & *can* we zoom?
        if event.GetWheelRotation() > 0:
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn((x, y))
        else:
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut((x, y))

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
            col_list = range(0, self.tiles.num_tiles_x)
            x_pix_start = -self.view_offset_x
        else:
            # Map > View - determine layout in X direction
            x_offset = self.view_offset_x + self.move_dx
            start_x_tile = int(x_offset / self.tile_size_x)
            stop_x_tile = ((x_offset + self.view_width + self.tile_size_x - 1)
                           / self.tile_size_x)
            stop_x_tile = int(stop_x_tile)
            col_list = range(start_x_tile, stop_x_tile)
            x_pix_start = start_x_tile * self.tile_size_y - x_offset

        if self.view_offset_y < 0:
            # View > Map in Y - centre in Y direction
            row_list = range(0, self.tiles.num_tiles_y)
            y_pix_start = -self.view_offset_y
        else:
            y_offset = self.view_offset_y + self.move_dy
            start_y_tile = int(y_offset / self.tile_size_y)
            stop_y_tile = ((y_offset + self.view_height
                            + self.tile_size_y - 1) / self.tile_size_y)
            stop_y_tile = int(stop_y_tile)
            row_list = range(start_y_tile, stop_y_tile)
            y_pix_start = start_y_tile * self.tile_size_y - y_offset

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
            dc.SetPen(wx.Pen(penclr, width=1))
            brushclr = wx.Colour(0, 0, 0)
            dc.SetBrush(wx.Brush(brushclr, style=wx.TRANSPARENT))
            dc.DrawRectangle(self.sbox_1_x, self.sbox_1_y,
                             self.sbox_w, self.sbox_h)

    ######
    # Miscellaneous
    ######

    def View2Geo(self, view_posn):
        """Convert a view coords position to a geo coords position.

        view_posn  tuple of view coords (xview, yview)

        Returns a tuple of geo coords (xgeo, ygeo);
        """

        (xview, yview) = view_posn
        xtile = float(self.view_offset_x + xview) / self.tile_size_x
        ytile = float(self.view_offset_y + yview) / self.tile_size_y
        return self.tiles.Tile2Geo(xtile, ytile)

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
        self.RecalcViewLonLatLimits()

    def RecalcViewLonLatLimits(self):
        """Recalculate the view geo extent values.

        Assumes only:
            self.view_offset_x
            self.view_offset_y
            self.tiles.tile_size_x
            self.tiles.tile_size_y
        values have been set.  All are map pixel values.
        """

        # get geo coords of top-left of view
        tltile_x = float(self.view_offset_x)/self.tiles.tile_size_x
        tltile_y = float(self.view_offset_y)/self.tiles.tile_size_y
        (self.view_llon, self.view_tlat) = self.tiles.Tile2Geo(tltile_x,
                                                               tltile_y)

        # then get geo coords of bottom-right of view
        tltile_x = float(self.view_offset_x+self.view_width)/self.tiles.tile_size_x
        tltile_y = float(self.view_offset_y+self.view_height)/self.tiles.tile_size_y
        (self.view_rlon, self.view_blat) = self.tiles.Tile2Geo(tltile_x,
                                                               tltile_y)

    def ZoomToLevel(self, level):
        """Use a new tile level.

        level  the new tile level to use.

        Returns True if all went well.
        Maintain centre of map, if possible.
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

    def GetMapCoordsFromView(self, posn):
        """Convert view pixel coordinates to map coordinates.

        posn  is a tuple (x, y) of view pixel coordinates

        Returns (x, y) map pixel coordinates.
        """

        # unpack the position
        (view_x, view_y) = posn

        # calculate map coords
        map_x = view_x + self.view_offset_x
        map_y = view_y + self.view_offset_y

        return (map_x, map_y)

    ######
    # Select helpers - get objects that were selected
    ######

    def GetNearestPointInLayer(self, layer, pt):
        """Determine if clicked location selects a point in layer data.

        layer  layer object we are looking in
        pt     click geo location if map-rel, else view coords

        Return None (no selection) or (point, data) of selected point where
        point is (xgeo,ygeo) or (xview,yview) depending on layer.map_rel.
        """

# TODO: speed this up?  Do we need to??
# http://en.wikipedia.org/wiki/Kd-tree
# would need to create kd-tree in AddLayer()

        (ptx, pty) = pt
        res = None
        dist = 9999999.0        # more than possible
        if layer.map_rel:
            for p in layer.data:
                (x, y, _, _, _, _, _, data) = p
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                if d < dist:
                    dist = d
                    res = ((x,y), data)

            if dist <= layer.delta:
                return res
        else:
            for p in layer.data:
                dc_w = self.view_width
                dc_h = self.view_height

                dc_w2 = dc_w / 2
                dc_h2 = dc_h / 2
                dc_h -= 1       # why?
                dc_w -= 1
                (x, y, place, _, _, x_off, y_off, data) = p
                exec self.point_view_placement[place]
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                if d < dist:
                    dist = d
                    res = ((x,y), data)

            if dist <= layer.delta:
                return res

        return None

    def GetBoxSelPointsInLayer(self, layer, p1, p2):
        """Get list of points inside box.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords, (x,y))
        p2     opposite corner point of selection box (tile coords, (x,y))

        We have to figure out which corner is which.

        Return a tuple (selection, data) where 'selection' is a list of
        selected point positions (xgeo,ygeo) and 'data' is a list of userdata
        objects associated withe selected points.

        If nothing is selected return None.
        """

#FIXME
# TODO: speed this up?  Do we need to??
        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        # get a list of points inside the selection box
        selection = []
        data = []

        if layer.map_rel:
            for p in layer.data:
                (x, y, _, _, _, _, _, udata) = p
                if lx <= x <= rx and by <= y <= ty:
                    selection.append((x, y))
                    data.append(udata)
        else:
            for p in layer.data:
                dc_w = self.view_width
                dc_h = self.view_height

                dc_w2 = dc_w / 2
                dc_h2 = dc_h / 2
                dc_h -= 1
                dc_w -= 1
                (x, y, place, _, _, x_off, y_off, udata) = p
                exec self.point_view_placement[place]
                if lx <= x <= rx and by <= y <= ty:
                    selection.append((x, y))
                    data.append(udata)

        if not selection:
            return None

        return (selection, data)

    def GetImageInLayer(self, layer, pt):
        """Decide if click location selects image object(s) in layer data.

        layer  layer object we are looking in
        pt     click location, either geo (lon, lat) or view (x, y)

        Returns either None if no selection or a tuple (selection, data)
        where 'selection' is a tuple (xgeo,ygeo) or (xview,yview) of the object
        placement point, 'data' is the data object associated with the selected
        object and 'relsel' is the relative position within the selected object
        of the mouse click.

        Note that there could conceivably be more than one image selectable in
        the layer at the mouse click position but only the first is selected.
        """

        (ptx, pty) = pt
        selection = None
        seldata = None

        log('GetImageInLayer: START, geo ptx=%s, geo pty=%s' % (str(ptx), str(pty)))

        # .data: [(x, y, bmap, w, h, placement, offset_x, offset_y, data),...]
        for p in layer.data:
            (x, y, _, w, h, placement, offset_x, offset_y, data) = p
            if layer.map_rel:
                # map-relative, ptx, pty, x, y are geo coords
                log('GetImageInLayer: calling .GeoExtent(x=%s, y=%s, placement=%s, w=%s, h=%s, offset_x=%s, offset_y=%s'
                        % (str(x), str(y), str(placement), str(w), str(h), str(offset_x), str(offset_y)))
                e = self.GeoExtent(x, y, placement, w, h, offset_x, offset_y)
                log('GetImageInLayer: GeoExtent() returned %s' % str(e))
                if e:
                    (llon, rlon, tlat, blat) = e
                    if llon <= ptx <= rlon and blat <= pty <= tlat:
                        selection = (x, y)
                        seldata = data
                        break
            else:
                # view_relative, ptx, pty, x, y are view coords
                e = self.ViewExtent(x, y, placement, w, h, offset_x, offset_y)
                (lv, rv, tv, bv) = e
                if lv <= ptx <= rv and tv <= pty <= bv:
                    selection = (ptx - lv, pty - tv)
                    seldata = data

        log('GetImageInLayer: FINISH, selection=%s' % str(selection))
        if selection:
            return (selection, seldata)
        return None
#FIXME must return relative selection

    def GetBoxSelImagesInLayer(self, layer, p1, p2):
        """Get list of images inside box p1-p2.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords, (x,y))
        p2     opposite corner point of selection box (tile coords, (x,y))

        We have to figure out which corner is which.

        Return a tuple (selection, data) where 'selection' is a list of
        selected point positions (xgeo,ygeo) and 'data' is a list of userdata
        objects associated withe selected points.

        If nothing is selected return None.
        """

        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        # now construct list of images inside box
        selection = []
        data = []
        for p in layer.data:
            log('####: layer.data=%s' % str(layer.data))
            (x, y, _, _, _, _, _, _, udata) = p
#            x = p[0]
#            y = p[1]
            if lx <= x <= rx and by <= y <= ty:
                selection.append((x, y))
                data.append(udata)

        if not selection:
            return None

        return (selection, data)

    def GetNearestPolygonInLayer(self, layer, pt):
        """Get all polygon objects clicked in layer data.

        layer  layer object we are looking in
        pt     click geo location (lon, lat)

        Returns a list of selection objects: ((x,y), udata).
        More than one polygon may be selected in a single click.
        """

        log('GetNearestPolygonInLayer: pt=%s' % str(pt))

        (ptx, pty) = pt
        result = []

        # (poly, placement, width, colour, close, filled, fillcolour, offset_x, offset_y, udata)
        for p in layer.data:
            (poly, placement, _, _, _, _, _, offset_x, offset_y, udata) = p
            if layer.map_rel:
                log('GetNearestPolygonInLayer: map-rel layer')
                # map-relative, all points are geo coordinates
                if self.point_in_poly_map(poly, ptx, pty,
                                          placement, offset_x, offset_y):
                    result.append((pt, udata))
            else:
                # view-relative, all points are view pixels
                if self.point_in_poly_view(poly, ptx, pty,
                                           placement, offset_x, offset_y):
                    result.append((pt, udata))

        return result

    def GetBoxSelPolygonsInLayer(self, layer, p1, p2):
        """Get list of polygons inside box p1-p2 in given layer.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords, (x,y))
        p2     opposite corner point of selection box (tile coords, (x,y))

        We have to figure out which corner is which.

        Return a tuple (selection, data) where 'selection' is a list of
        iterables of vertex positions and 'data' is  list of data objects
        associated with each polygon selected.
        """

        selection = []
        data = []

# FIXME

        return (selection, data)

    def GetNearestTextInLayer(self, layer, pt):
        """Determine if clicked location selects a text object in layer data.

        layer  layer object we are looking in
        pt     click view location (xview, yview)

        Return None (no selection) or data for closest text.

        Just search for text 'hotspot' - just like point select.
        Later make text sensitive (need text extent data).
        """

        (ptx, pty) = pt
        res = None
        delta = layer.delta

        log('GetNearestTextInLayer: ptx=%s, pty=%s, delta=%s' % (str(ptx), str(pty), str(delta)))

        if layer.map_rel:
            for p in layer.data:
                (x, y, _, _, _, _, _, _, _, _, _, data) = p
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                if d < delta:
                    res = ((x,y), data)
                    break
        else:       # view-rel
            for p in layer.data:
                dc_w = self.view_width
                dc_h = self.view_height

                dc_w2 = dc_w / 2
                dc_h2 = dc_h / 2
                dc_h -= 1       # why?
                dc_w -= 1
                (x, y, _, place, _, _, _, _, _, x_off, y_off, data) = p
                exec self.point_view_placement[place]
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                log('GetNearestTextInLayer: checking, x=%s, y=%s, place=%s, x_off=%s, y_off=%s, d=%s'
                        % (str(x), str(y), str(place), str(x_off), str(y_off), str(d)))
                if d < delta:
                    res = ((x,y), data)
                    break

        log('GetNearestTextInLayer: returning %s' % str(res))
        return res

#        (ptx, pty) = pt
#        log('GetNearestTextInLayer: ptx=%s, pty=%s' % (str(ptx), str(pty)))
#        selection = []
#        data = []
#        dist = 1.0E+100        # more than possible
#        delta = layer.delta
#        for p in layer.data:
#            (x, y, _, _, _, _, _, _, _, _, _, data) = p
#            d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
#            log('GetNearestTextInLayer: x=%s, y=%s, d=%s, delta=%s' % (str(x), str(y), str(d), str(delta)))
#            if d < delta:
#                dist = d
#                selection = (x, y)
#                data = udata
#                break
#
#        if selection:
#            return (selection, data)
#        return None

    def GetBoxSelTextsInLayer(self, layer, p1, p2):
        """Get list of text objects inside box p1-p2.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (geo coords, (x,y))
        p2     opposite corner point of selection box (geo coords, (x,y))

        We have to figure out which corner is which.

        Returns (selection, data) where 'selection' is a list of text positions
        (xgeo,ygeo) and 'data' is a list of userdata objects associated with the
        selected text objects.  Returns None if no selection.
        """

        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        # get a list of points inside the selection box
        selection = []
        data = []

        for p in layer.data:
            (x, y, _, _, _, _, _, _, _, _, _, udata) = p
            if lx <= x <= rx and by <= y <= ty:
                selection.append((x, y))
                data.append(udata)

        # return appropriate result
        if selection:
            return (selection, data)
        return None

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

    def ZoomIn(self, xy):
        """Zoom map in to the next level.

        xy  is a tuple (x, y) of pixel coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # predict the view offset after zoom
        (map_x, map_y) = self.GetMapCoordsFromView(xy)
        self.view_offset_x = map_x * 2 - self.view_width / 2
        self.view_offset_y = map_y * 2 - self.view_height / 2

        # set some internal state through resize code
        self.ResizeCallback()

        self.Update()

    def ZoomOut(self, xy):
        """Zoom map out to the previous level.

        xy  is a tuple (x, y) of pixel coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # set view state
        (map_x, map_y) = self.GetMapCoordsFromView(xy)
        self.view_offset_x = map_x / 2 - self.view_width / 2
        self.view_offset_y = map_y / 2 - self.view_height / 2

        # set some internal state through size code
        self.ResizeCallback()

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

        log('RaiseEventPosition: mposn=%s, vposn=%s'
                % (str(mposn), str(vposn)))

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

    def RaiseEventSelect(self, mposn, vposn, layer=None, selection=None, data=None):
        """Raise a point SELECT event.

        mposn      map coordinates of the mouse click
        vposn      view coordinates of the mouse click
        layer      layer the select was on
        selection  None if no selection or a tuple (point, data) where 'point'
                   is the selected object point ((xgeo,ygeo) or (xview,yview))
                   and data is the associated data object

        This event is raised even when nothing is selected.  In that case,
        event.layer_id, .selection and .data are None and .mposn and
        .vposn are the mouse click positions.
        """

        event = _PySlipEvent(_myEVT_PYSLIP_SELECT, self.GetId())

        event.type = EventSelect
        event.mposn = mposn
        event.vposn = vposn
        event.layer_id = layer.id
        event.selection = None
        event.data = None
        if selection:
            (event.selection, event.data) = selection

        log('RaiseEventSelect: .type=%s, .layer_id=%d, .mposn=%s, .vposn=%s, .selection=%s, .data=%s'
                % (str(EventSelect), layer.id, str(event.mposn), str(event.vposn), str(event.selection), str(event.data)))

        self.GetEventHandler().ProcessEvent(event)

    def RaiseEventBoxSelect(self, layer=None, selection=None, data=None):
        """Raise a point BOXSELECT event.

        layer      layer the select was on
        selection  list of selected object points [(x,y), ...] (geo or view)
        data       a list of data objects associated with the selected points

        This event is raised even when nothing is selected.  In that case,
        event.layer_id, .selection and .data are None.
        """

        event = _PySlipEvent(_myEVT_PYSLIP_BOXSELECT, self.GetId())

        event.type = EventBoxSelect
        event.layer_id = layer.id
        event.selection = selection
        event.data = data

        log('RaiseEventBoxSelect: .type=%s, .layer_id=%d, .selection=%s, .data=%s'
                % (str(EventBoxSelect), event.layer_id, str(event.selection), str(event.data)))

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

        log('RaiseEventPolySelect: .type=%s, .layer_id=%d, .mposn=%s, .vposn=%s, .selection=%s, .data=%s'
                % (str(EventPolySelect), event.layer_id, str(event.mposn), str(event.vposn), str(selection), str(data)))

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

        log('RaiseEventPolyBoxSelect: .type=%s, .layer_id=%d, .selection=%s, .data=%s'
                % (str(EventPolyBoxSelect), layer.id, str(event.selection), str(event.data)))

        self.GetEventHandler().ProcessEvent(event)

    ######
    # Various pySlip utility routines
    ######

    def point_in_poly_map(self, poly, ptx, pty, placement, offset_x, offset_y):
        """Decide if a point is inside a map-relative polygon.

        poly       an iterable of (x,y) where x,y are in geo coordinates
        ptx        point X coordinate (geo)
        pty        point Y coordinate (geo)
        placement  a placement string
        offset_x   X offset in pixels
        offset_y   Y offset in pixels

        The (ptx, pty) point, while in geo coordinates, must be a click point
        within the view.

        Returns True if point is inside the polygon.
        """

        log('point_in_poly_map: ptx=%s, pty=%s' % (str(ptx), str(pty)))

        return point_inside_polygon(ptx, pty, poly)

#        # convert polygon and placement into list of (x,y) tuples
#        view_poly = []
#        for (gx, gy) in poly:
#            log('BEFORE: gx=%s, gy=%s' % (str(gx), str(gy)))
#            (tx, ty) =  self.tiles.Geo2Tile(gx, gy)             # tx=10.2, ty=6.1
#            log('DURING: tx=%s, ty=%s' % (str(tx), str(ty)))
#            x = tx*self.tiles.tile_size_x - self.view_offset_x
#            y = ty*self.tiles.tile_size_y - self.view_offset_y
#            log('DURING: x=%s, y=%s, .view_offset_x=%d, .view_offset_y=%d'
#                % (str(x), str(y), self.view_offset_x, self.view_offset_y))
#            exec self.point_map_placement[placement]
#            log('AFTER: x=%s, y=%s, placement=%s' % (str(x), str(y), str(placement)))
#            view_poly.append((x, y))

        # decide if (ptx,pty) is inside polygon
        result = point_inside_polygon(ptx, pty, view_poly)
        log('RETURN: %s' % str(result))
        return result

    def point_in_poly_view(self, poly, ptx, pty, placement, offset_x, offset_y):
        """Decide if a point is inside a view-relative polygon.

        poly       an iterable of (x,y) where x,y are in view (pixel) coordinates
        ptx        point X coordinate (view)
        pty        point Y coordinate (view)
        placement  a placement string
        offset_x   X offset in pixels
        offset_y   Y offset in pixels

        Returns True if point is inside the polygon.
        """

        # convert polygon and placement into list of (x,y) tuples
        view_poly = []
        for (gx, gy) in poly:
            log('BEFORE: gx=%s, gy=%s' % (str(gx), str(gy)))
            (tx, ty) =  self.tiles.Geo2Tile(gx, gy)
            log('DURING: tx=%s, ty=%s' % (str(tx), str(ty)))
            x = tx - self.view_offset_x
            y = ty - self.view_offset_y
            log('DURING: x=%s, y=%s' % (str(x), str(y)))
            exec self.point_map_placement[placement]
            log('AFTER: x=%s, y=%s' % (str(x), str(y)))
            view_poly.append((x, y))

        # decide if (ptx,pty) is inside polygon
        return point_inside_polygon(ptx, pty, view_poly)

    def GeoExtent(self, lon, lat, placement, w, h, x_off, y_off):
        """Get geo extent of area.

        lon, lat      geo coords of position to place area at
        placement     placement string ('cc', 'se', etc)
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

# FIXME

        log('GeoExtent: lon=%s, lat=%s, placement=%s, w=%s, h=%s, x_off=%s, y_off=%s'
                % (str(lon), str(lat), str(placement), str(w), str(h), str(x_off), str(y_off)))

        # decide if object CAN be in view
        # check point in lower, right or lower-right quadrants
        if self.view_rlon < lon or self.view_blat > lat:
            log('GeoExtent: object not in view, returning None')
            return None

        # now, figure out point view position from (lon, lat)
        (x, y) = self.tiles.Geo2Tile(lon, lat)
        log('GeoExtent: point tile coords x=%s, y=%s' % (str(x), str(y)))
        x = x*self.tile_size_x - self.view_offset_x
        y = y*self.tile_size_y - self.view_offset_y
        log('GeoExtent: object point map pixels x=%s, y=%s' % (str(x), str(y)))

        # then do the placement of the top_left point
        w2 = w/2.0
        h2 = h/2.0
        exec self.point_map_perturb[placement]
        (tlx, tly) = (x, y)
        log('GeoExtent: extent TL point (view) tlx=%s, tly=%s' % (str(tlx), str(tly)))

        # now get bottom_right corner in pixel coords
        brx = tlx + w
        bry = tly + h
        log('GeoExtent: extent BR point (view) brx=%s, bry=%s' % (str(brx), str(bry)))

        # decide if object is off view or not
        log('####: brx < -w = %s < %s = %s' % (str(brx), str(-w), str(brx < -w)))
        log('####: bry < -h = %s < %s = %s' % (str(bry), str(-h), str(bry < -h)))
        log('####: tlx > self.view_width = %s > %s = %s' % (str(tlx), str(self.view_width), str(tlx > self.view_width)))
        log('####: tly > self.view_height = %s > %s = %s' % (str(tly), str(self.view_height), str(tly > self.view_height)))

        if (brx < -w or bry < -h
                or tlx > self.view_width or tly > self.view_height):
            log('GeoExtent: object not in view, returning None')
            return None

        # return geo extent
        (llon, tlat) = self.View2Geo((tlx, tly))
        (rlon, blat) = self.View2Geo((brx, bry))
        log('GeoExtent: returning (%s, %s, %s, %s)'
                % (str(llon), str(rlon), str(tlat), str(blat)))

        return (llon, rlon, tlat, blat)

    def ViewExtent(self, x, y, placement, w, h, x_off, y_off):
        """Get view extent of area.

        x, y          view coords of position to place area at
        placement     placement string ('cc', 'se', etc)
        w, h          area width and height (pixels)
        x_off, y_off  x and y offset (pixels)

        Return the view extent of the area: (left, right, top, bottom)
        where:
            left    pixel coords of left side of area
            right   pixel coords of right side of area
            top     pixel coords of top of area
            bottom  pixel coords of bottom of area

        If area is 'off view' limit extent to view boundary.  If area is totally
        off the view, return a zero width/height area at a view corner.
        """

        # prepare the half values, etc
        dc_w = float(self.view_width)
        dc_w2 = dc_w/2.0
        dc_h = float(self.view_height)
        dc_h2 = dc_h/2.0
        w2 = w/2.0
        h2 = h/2.0

        # top left corner
        exec self.point_view_perturb[placement]
        (left, top) = (x, y)

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

