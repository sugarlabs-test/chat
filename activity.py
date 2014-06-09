#Copyright 2007-2008 One Laptop Per Child
#Copyright 2009-14 Sugar Labs
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

from gi.repository import Gtk
from gi.repository import Gdk

try:
    from gi.repository import Gst
    _HAS_SOUND = True
except:
    _HAS_SOUND = False

import logging
import json
import math
import os
import subprocess
import time
from gettext import gettext as _

from telepathy.interfaces import CHANNEL_INTERFACE
from telepathy.interfaces import CHANNEL_INTERFACE_GROUP
from telepathy.interfaces import CHANNEL_TYPE_TEXT
from telepathy.interfaces import CONN_INTERFACE_ALIASING
from telepathy.constants import CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES
from telepathy.constants import CHANNEL_TEXT_MESSAGE_TYPE_NORMAL
from telepathy.client import Connection
from telepathy.client import Channel

from sugar3.graphics import style
from sugar3.graphics.alert import NotifyAlert
from sugar3.graphics.palette import Palette
from sugar3.graphics.toolbarbox import ToolbarBox
from sugar3.activity import activity
from sugar3.activity.activity import get_bundle_path
from sugar3.presence import presenceservice
from sugar3.activity.widgets import ActivityToolbarButton
from sugar3.activity.widgets import StopButton
from sugar3.activity.widgets import RadioMenuButton
from sugar3.activity.activity import get_activity_root
from sugar3.activity.activity import show_object_in_journal
from sugar3.datastore import datastore
from sugar3 import profile

from chat import smilies
from chat.box import ChatBox


logger = logging.getLogger('chat-activity')

SMILIES_COLUMNS = 7

if _HAS_SOUND:
    Gst.init([])


def _is_tablet_mode():
    if not os.path.exists('/dev/input/event4'):
        return False
    try:
        output = subprocess.call(
            ['evtest', '--query', '/dev/input/event4', 'EV_SW',
             'SW_TABLET_MODE'])
    except (OSError, subprocess.CalledProcessError):
        return False
    if str(output) == '10':
        return True
    return False


# pylint: disable-msg=W0223
class Chat(activity.Activity):

    def __init__(self, handle):
        smilies.init()

        pservice = presenceservice.get_instance()
        self.owner = pservice.get_owner()

        self.chatbox = ChatBox(self.owner)
        self.chatbox.connect('open-on-journal', self.__open_on_journal)

        super(Chat, self).__init__(handle)

        self.entry = None

        root = self.make_root()
        self.set_canvas(root)
        root.show_all()
        self.entry.grab_focus()

        toolbar_box = ToolbarBox()
        self.set_toolbar_box(toolbar_box)

        self._activity_toolbar_button = ActivityToolbarButton(self)
        toolbar_box.toolbar.insert(self._activity_toolbar_button, 0)
        self._activity_toolbar_button.show()

        separator = Gtk.SeparatorToolItem()
        toolbar_box.toolbar.insert(separator, -1)

        self._smiley = RadioMenuButton(icon_name='smilies')
        self._smiley.palette = Palette(_('Insert smiley'))
        self._smiley.props.sensitive = False
        toolbar_box.toolbar.insert(self._smiley, -1)

        table = self._create_pallete_smiley_table()
        table.show_all()
        self._smiley.palette.set_content(table)

        separator = Gtk.SeparatorToolItem()
        separator.props.draw = False
        separator.set_expand(True)
        toolbar_box.toolbar.insert(separator, -1)

        toolbar_box.toolbar.insert(StopButton(self), -1)
        toolbar_box.show_all()

        # Chat is room or one to one:
        self._chat_is_room = False
        self.text_channel = None

        if _HAS_SOUND:
            self.element = Gst.ElementFactory.make('playbin', 'Player')

        if self.shared_activity:
            # we are joining the activity
            self.connect('joined', self._joined_cb)
            if self.get_shared():
                # we have already joined
                self._joined_cb(self)
        elif handle.uri:
            # XMPP non-sugar3 incoming chat, not sharable
            self._activity_toolbar_button.props.page.share.props.visible = False
            self._one_to_one_connection(handle.uri)
        else:
            # we are creating the activity
            if not self.metadata or self.metadata.get(
                    'share-scope', activity.SCOPE_PRIVATE) == \
                    activity.SCOPE_PRIVATE:
                # if we are in private session
                self._alert(_('Off-line'), _('Share, or invite someone.'))
            self.connect('shared', self._shared_cb)

    def _create_pallete_smiley_table(self):
        row_count = int(math.ceil(len(smilies.THEME) / float(SMILIES_COLUMNS)))
        table = Gtk.Table(rows=row_count, columns=SMILIES_COLUMNS)
        index = 0

        for y in range(row_count):
            for x in range(SMILIES_COLUMNS):
                if index >= len(smilies.THEME):
                    break
                path, hint, codes = smilies.THEME[index]
                image = Gtk.Image()
                image.set_from_file(path)
                button = Gtk.ToolButton()
                button.set_icon_widget(image)
                button.connect('clicked', self._add_smiley_to_entry, codes[0])
                table.attach(button, x, x + 1, y, y + 1)
                button.show()
                index = index + 1
        return table

    def _add_smiley_to_entry(self, button, text):
        self._smiley.palette.popdown(True)
        pos = self.entry.props.cursor_position
        self.entry.insert_text(text, pos)
        self.entry.grab_focus()
        self.entry.set_position(pos + len(text))

    def _shared_cb(self, sender):
        logger.debug('Chat was shared')
        self._setup()

    def _one_to_one_connection(self, tp_channel):
        '''Handle a private invite from a non-sugar3 XMPP client.'''
        if self.shared_activity or self.text_channel:
            return
        bus_name, connection, channel = json.loads(tp_channel)
        logger.debug('GOT XMPP: %s %s %s', bus_name, connection, channel)
        Connection(bus_name, connection, ready_handler=lambda conn:
                   self._one_to_one_connection_ready_cb(
                       bus_name, channel, conn))

    def _one_to_one_connection_ready_cb(self, bus_name, channel, conn):
        '''Callback for Connection for one to one connection'''
        text_channel = Channel(bus_name, channel)
        self.text_channel = TextChannelWrapper(text_channel, conn)
        self.text_channel.set_received_callback(self._received_cb)
        self.text_channel.handle_pending_messages()
        self.text_channel.set_closed_callback(
            self._one_to_one_connection_closed_cb)
        self._chat_is_room = False
        self._alert(_('On-line'), _('Private Chat'))

        # XXX How do we detect the sender going offline?
        self.entry.set_sensitive(True)
        self.entry.grab_focus()

    def _one_to_one_connection_closed_cb(self):
        '''Callback for when the text channel closes.'''
        self._alert(_('Off-line'), _('left the chat'))

    def _setup(self):
        self.text_channel = TextChannelWrapper(
            self.shared_activity.telepathy_text_chan,
            self.shared_activity.telepathy_conn)
        self.text_channel.set_received_callback(self._received_cb)
        self._alert(_('On-line'), _('Connected'))
        self.shared_activity.connect('buddy-joined', self._buddy_joined_cb)
        self.shared_activity.connect('buddy-left', self._buddy_left_cb)
        self._chat_is_room = True
        self.entry.set_sensitive(True)
        self.entry.grab_focus()
        self._smiley.props.sensitive = True

    def _joined_cb(self, sender):
        '''Joined a shared activity.'''
        if not self.shared_activity:
            return
        logger.debug('Joined a shared chat')
        for buddy in self.shared_activity.get_joined_buddies():
            self._buddy_already_exists(buddy)
        self._setup()

    def _received_cb(self, buddy, text):
        '''Show message that was received.'''
        if buddy:
            if type(buddy) is dict:
                nick = buddy['nick']
            else:
                nick = buddy.props.nick
        else:
            nick = '???'
        logger.debug('Received message from %s: %s', nick, text)
        self.chatbox.add_text(buddy, text)

        if self.owner.props.nick in text:
            self.play_sound('said_nick')

        vscroll = self.chatbox.get_vadjustment()
        if vscroll.get_property('value') != vscroll.get_property('upper'):
            self._alert(_('New message'), _('New message from %s' % nick))
        # if not self.has_focus:
        #     self.notify_user(_('Message from %s') % buddy, text)

    def _alert(self, title, text=None):
        alert = NotifyAlert(timeout=5)
        alert.props.title = title
        alert.props.msg = text
        self.add_alert(alert)
        alert.connect('response', self._alert_cancel_cb)
        alert.show()

    def _alert_cancel_cb(self, alert, response_id):
        self.remove_alert(alert)

    def __open_on_journal(self, widget, url):
        '''Ask the journal to display a URL'''
        logging.debug('Create journal entry for URL: %s', url)
        jobject = datastore.create()
        metadata = {
            'title': '%s: %s' % (_('URL from Chat'), url),
            'title_set_by_user': '1',
            'icon-color': profile.get_color().to_string(),
            'mime_type': 'text/uri-list',
            }
        for k, v in metadata.items():
            jobject.metadata[k] = v
        file_path = os.path.join(get_activity_root(), 'instance',
                                 '%i_' % time.time())
        open(file_path, 'w').write(url + '\r\n')
        os.chmod(file_path, 0755)
        jobject.set_file_path(file_path)
        datastore.write(jobject)
        show_object_in_journal(jobject.object_id)
        jobject.destroy()
        os.unlink(file_path)

    def _buddy_joined_cb(self, sender, buddy):
        '''Show a buddy who joined'''
        if buddy == self.owner:
            return
        self.chatbox.add_text(buddy,
                              _('%s joined the chat') % buddy.props.nick,
                              status_message=True)

        self.play_sound('login')

    def _buddy_left_cb(self, sender, buddy):
        '''Show a buddy who joined'''
        if buddy == self.owner:
            return
        self.chatbox.add_text(buddy, _('%s left the chat') % buddy.props.nick,
                              status_message=True)

        self.play_sound('logout')

    def _buddy_already_exists(self, buddy):
        '''Show a buddy already in the chat.'''
        if buddy == self.owner:
            return
        self.chatbox.add_text(buddy, _('%s is here') % buddy.props.nick,
                              status_message=True)

    def can_close(self):
        '''Perform cleanup before closing.

        Close text channel of a one to one XMPP chat.
        '''
        if self._chat_is_room is False:
            if self.text_channel is not None:
                self.text_channel.close()
        return True

    def make_root(self):
        # filler used to move entry box to accomodate OSK
        self._filler = [Gtk.VBox(), Gtk.VBox()]
        # ToDo: calculate filler sizes
        self._filler[0].set_size_request(-1, 160)
        self._filler[1].set_size_request(-1, 80)

        entry = Gtk.Entry()
        entry.modify_bg(Gtk.StateType.INSENSITIVE,
                        style.COLOR_WHITE.get_gdk_color())
        entry.modify_base(Gtk.StateType.INSENSITIVE,
                          style.COLOR_WHITE.get_gdk_color())
        entry.set_sensitive(False)
        entry.connect('focus-in-event', self.entry_focus_in_cb)
        entry.connect('focus-out-event', self.entry_focus_out_cb)
        entry.connect('activate', self.entry_activate_cb)
        entry.connect('key-press-event', self.entry_key_press_cb)
        self.entry = entry

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hbox.pack_start(self.entry, True, True, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, homogeneous=False)
        box.pack_start(self.chatbox, True, True, 0)
        box.pack_start(hbox, False, True, 0)
        if _is_tablet_mode():
            box.pack_start(self._filler[0], True, True, 0)
            box.pack_start(self._filler[1], True, True, 0)

        return box

    def entry_focus_in_cb(self, entry, event):
        logging.error('focus in')
        if _is_tablet_mode():
            if Gdk.Screen.width() > Gdk.Screen.height():
                self._filler[0].show()
            else:
                self._filler[1].show()

    def entry_focus_out_cb(self, entry, event):
        logging.error('focus out')
        if _is_tablet_mode():
            self._filler[0].hide()
            self._filler[1].hide()

    def entry_key_press_cb(self, widget, event):
        '''Check for scrolling keys.

        Check if the user pressed Page Up, Page Down, Home or End and
        scroll the window according the pressed key.
        '''
        vadj = self.chatbox.get_vadjustment()
        if event.keyval == Gdk.KEY_Page_Down:
            value = vadj.get_value() + vadj.page_size
            if value > vadj.upper - vadj.page_size:
                value = vadj.upper - vadj.page_size
            vadj.set_value(value)
        elif event.keyval == Gdk.KEY_Page_Up:
            vadj.set_value(vadj.get_value() - vadj.page_size)
        elif event.keyval == Gdk.KEY_Home and \
             event.get_state() & Gdk.ModifierType.CONTROL_MASK:
            vadj.set_value(vadj.lower)
        elif event.keyval == Gdk.KEY_End and \
             event.get_state() & Gdk.ModifierType.CONTROL_MASK:
            vadj.set_value(vadj.upper - vadj.page_size)

    def entry_activate_cb(self, entry):
        self.chatbox._scroll_auto = True

        text = entry.props.text
        logger.debug('Entry: %s' % text)
        if text:
            self.chatbox.add_text(self.owner, text)
            entry.props.text = ''
            if self.text_channel:
                self.text_channel.send(text)
            else:
                logger.debug('Tried to send message but text channel '
                             'not connected.')

    def write_file(self, file_path):
        '''Store chat log in Journal.

        Handling the Journal is provided by Activity - we only need
        to define this method.
        '''
        logger.debug('write_file: writing %s' % file_path)
        self.chatbox.add_log_timestamp()
        f = open(file_path, 'w')
        try:
            f.write(self.chatbox.get_log())
        finally:
            f.close()
        self.metadata['mime_type'] = 'text/plain'

    def read_file(self, file_path):
        '''Load a chat log from the Journal.
        Handling the Journal is provided by Activity - we only need
        to define this method.
        '''
        logger.debug('read_file: reading %s' % file_path)
        log = open(file_path).readlines()
        last_line_was_timestamp = False
        for line in log:
            if line.endswith('\t\t\n'):
                if last_line_was_timestamp is False:
                    timestamp = line.strip().split('\t')[0]
                    self.chatbox.add_separator(timestamp)
                    last_line_was_timestamp = True
            else:
                timestamp, nick, color, status, text = line.strip().split('\t')
                status_message = bool(int(status))
                self.chatbox.add_text({'nick': nick, 'color': color},
                                      text, status_message)
                last_line_was_timestamp = False

    def play_sound(self, event):
        if _HAS_SOUND:
            SOUNDS_PATH = os.path.join(get_bundle_path(), 'sounds')
            SOUNDS = {'said_nick': os.path.join(SOUNDS_PATH, 'alert.wav'),
                      'login': os.path.join(SOUNDS_PATH, 'login.wav'),
                      'logout': os.path.join(SOUNDS_PATH, 'logout.wav')}

            self.element.set_state(Gst.State.NULL)
            self.element.set_property('uri', 'file://%s' % SOUNDS[event])
            self.element.set_state(Gst.State.PLAYING)


class TextChannelWrapper(object):
    '''Wrap a telepathy Text Channfel to make usage simpler.'''

    def __init__(self, text_chan, conn):
        '''Connect to the text channel'''
        self._activity_cb = None
        self._activity_close_cb = None
        self._text_chan = text_chan
        self._conn = conn
        self._logger = logging.getLogger(
            'chat-activity.TextChannelWrapper')
        self._signal_matches = []
        m = self._text_chan[CHANNEL_INTERFACE].connect_to_signal(
            'Closed', self._closed_cb)
        self._signal_matches.append(m)

    def send(self, text):
        '''Send text over the Telepathy text channel.'''
        # XXX Implement CHANNEL_TEXT_MESSAGE_TYPE_ACTION
        if self._text_chan is not None:
            self._text_chan[CHANNEL_TYPE_TEXT].Send(
                CHANNEL_TEXT_MESSAGE_TYPE_NORMAL, text)

    def close(self):
        '''Close the text channel.'''
        self._logger.debug('Closing text channel')
        try:
            self._text_chan[CHANNEL_INTERFACE].Close()
        except Exception:
            self._logger.debug('Channel disappeared!')
            self._closed_cb()

    def _closed_cb(self):
        '''Clean up text channel.'''
        self._logger.debug('Text channel closed.')
        for match in self._signal_matches:
            match.remove()
        self._signal_matches = []
        self._text_chan = None
        if self._activity_close_cb is not None:
            self._activity_close_cb()

    def set_received_callback(self, callback):
        '''Connect the function callback to the signal.

        callback -- callback function taking buddy and text args
        '''
        if self._text_chan is None:
            return
        self._activity_cb = callback
        m = self._text_chan[CHANNEL_TYPE_TEXT].connect_to_signal(
            'Received', self._received_cb)
        self._signal_matches.append(m)

    def handle_pending_messages(self):
        '''Get pending messages and show them as received.'''
        for identity, timestamp, sender, type_, flags, text in \
            self._text_chan[
                CHANNEL_TYPE_TEXT].ListPendingMessages(False):
            self._received_cb(identity, timestamp, sender, type_, flags, text)

    def _received_cb(self, identity, timestamp, sender, type_, flags, text):
        '''Handle received text from the text channel.

        Converts sender to a Buddy.
        Calls self._activity_cb which is a callback to the activity.
        '''
        if type_ != 0:
            # Exclude any auxiliary messages
            return

        if self._activity_cb:
            try:
                self._text_chan[CHANNEL_INTERFACE_GROUP]
            except Exception:
                # One to one XMPP chat
                nick = self._conn[
                    CONN_INTERFACE_ALIASING].RequestAliases([sender])[0]
                buddy = {'nick': nick, 'color': '#000000,#808080'}
            else:
                # Normal sugar3 MUC chat
                # XXX: cache these
                buddy = self._get_buddy(sender)
            self._activity_cb(buddy, text)
            self._text_chan[
                CHANNEL_TYPE_TEXT].AcknowledgePendingMessages([identity])
        else:
            self._logger.debug('Throwing received message on the floor'
                               ' since there is no callback connected. See'
                               ' set_received_callback')

    def set_closed_callback(self, callback):
        '''Connect a callback for when the text channel is closed.

        callback -- callback function taking no args

        '''
        self._activity_close_cb = callback

    def _get_buddy(self, cs_handle):
        '''Get a Buddy from a (possibly channel-specific) handle.'''
        # XXX This will be made redundant once Presence Service
        # provides buddy resolution
        # Get the Presence Service
        pservice = presenceservice.get_instance()
        # Get the Telepathy Connection
        tp_name, tp_path = pservice.get_preferred_connection()
        conn = Connection(tp_name, tp_path)
        group = self._text_chan[CHANNEL_INTERFACE_GROUP]
        my_csh = group.GetSelfHandle()
        if my_csh == cs_handle:
            handle = conn.GetSelfHandle()
        elif group.GetGroupFlags() & \
             CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES:
            handle = group.GetHandleOwners([cs_handle])[0]
        else:
            handle = cs_handle

            # XXX: deal with failure to get the handle owner
            assert handle != 0

        return pservice.get_buddy_by_telepathy_handle(
            tp_name, tp_path, handle)
