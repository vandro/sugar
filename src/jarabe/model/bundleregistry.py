# Copyright (C) 2006-2007 Red Hat, Inc.
# Copyright (C) 2009 Aleksey Lim
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

import os
import logging
import traceback
import sys

import gobject
import gio
import cjson

from sugar.bundle.activitybundle import ActivityBundle
from sugar.bundle.contentbundle import ContentBundle
from sugar.bundle.bundle import MalformedBundleException, \
    AlreadyInstalledException, RegistrationException
from sugar import env

from jarabe import config

class BundleRegistry(gobject.GObject):
    """Tracks the available activity bundles"""

    __gsignals__ = {
        'bundle-added':   (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                           ([gobject.TYPE_PYOBJECT])),
        'bundle-removed': (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                           ([gobject.TYPE_PYOBJECT])),
        'bundle-changed': (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                           ([gobject.TYPE_PYOBJECT]))
    }

    def __init__(self):
        logging.debug('STARTUP: Loading the bundle registry')
        gobject.GObject.__init__(self)

        self._mime_defaults = self._load_mime_defaults()

        self._bundles = []
        user_path = env.get_user_activities_path()
        for activity_dir in [user_path, config.activities_path]:
            self._scan_directory(activity_dir)
            directory = gio.File(activity_dir)
            monitor = directory.monitor_directory()
            monitor.connect('changed', self.__file_monitor_changed_cb)

        self._last_defaults_mtime = -1
        self._favorite_bundles = {}

        try:
            self._load_favorites()
        except Exception:
            logging.error('Error while loading favorite_activities\n%s.' \
                    % traceback.format_exc())

        self._merge_default_favorites()

    def __file_monitor_changed_cb(self, monitor, one_file, other_file,
                                  event_type):
        if not one_file.get_path().endswith('.activity'):
            return
        if event_type == gio.FILE_MONITOR_EVENT_CREATED:
            self.add_bundle(one_file.get_path())
        elif event_type == gio.FILE_MONITOR_EVENT_DELETED:
            self.remove_bundle(one_file.get_path())

    def _load_mime_defaults(self):
        defaults = {}

        f = open(os.path.join(config.data_path, 'mime.defaults'), 'r')
        for line in f.readlines():
            line = line.strip()
            if line and not line.startswith('#'):
                mime = line[:line.find(' ')]
                handler = line[line.rfind(' ') + 1:]
                defaults[mime] = handler
        f.close()

        return defaults

    def _get_favorite_key(self, bundle_id, version):
        """We use a string as a composite key for the favorites dictionary
        because JSON doesn't support tuples and python won't accept a list
        as a dictionary key.
        """
        if ' ' in bundle_id:
            raise ValueError('bundle_id cannot contain spaces')
        return '%s %s' % (bundle_id, version)

    def _load_favorites(self):
        favorites_path = env.get_profile_path('favorite_activities')
        if os.path.exists(favorites_path):
            favorites_data = cjson.decode(open(favorites_path).read())

            favorite_bundles = favorites_data['favorites']
            if not isinstance(favorite_bundles, dict):
                raise ValueError('Invalid format in %s.' % favorites_path)
            if favorite_bundles:
                first_key = favorite_bundles.keys()[0]
                if not isinstance(first_key, basestring):
                    raise ValueError('Invalid format in %s.' % favorites_path)

                first_value = favorite_bundles.values()[0]
                if first_value is not None and \
                   not isinstance(first_value, dict):
                    raise ValueError('Invalid format in %s.' % favorites_path)

            self._last_defaults_mtime = float(favorites_data['defaults-mtime'])
            self._favorite_bundles = favorite_bundles

    def _merge_default_favorites(self):
        default_activities = []
        defaults_path = os.path.join(config.data_path, 'activities.defaults')
        if os.path.exists(defaults_path):
            file_mtime = os.stat(defaults_path).st_mtime
            if file_mtime > self._last_defaults_mtime:
                f = open(defaults_path, 'r')
                for line in f.readlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        default_activities.append(line)
                f.close()
                self._last_defaults_mtime = file_mtime

        if not default_activities:
            return

        for bundle_id in default_activities:
            max_version = -1
            for bundle in self._bundles:
                if bundle.get_bundle_id() == bundle_id and \
                        max_version < bundle.get_activity_version():
                    max_version = bundle.get_activity_version()

            key = self._get_favorite_key(bundle_id, max_version)
            if max_version > -1 and key not in self._favorite_bundles:
                self._favorite_bundles[key] = None

        logging.debug('After merging: %r' % self._favorite_bundles)

        self._write_favorites_file()

    def get_bundle(self, bundle_id):
        """Returns an bundle given his service name"""
        for bundle in self._bundles:
            if bundle.get_bundle_id() == bundle_id:
                return bundle
        return None
    
    def __iter__(self):
        return self._bundles.__iter__()

    def _scan_directory(self, path):
        if not os.path.isdir(path):
            return

        # Sort by mtime to ensure a stable activity order
        bundles = {}
        for f in os.listdir(path):
            if not f.endswith('.activity'):
                continue
            try:
                bundle_dir = os.path.join(path, f)
                if os.path.isdir(bundle_dir):
                    bundles[bundle_dir] = os.stat(bundle_dir).st_mtime
            except Exception, e:
                logging.error('Error while processing installed activity ' \
                              'bundle: %s, %s, %s' % (f, e.__class__, e))

        bundle_dirs = bundles.keys()
        bundle_dirs.sort(lambda d1, d2: cmp(bundles[d1], bundles[d2]))
        for folder in bundle_dirs:
            try:
                self._add_bundle(folder)
            except Exception, e:
                logging.error('Error while processing installed activity ' \
                              'bundle: %s, %s, %s' % (folder, e.__class__, e))

    def add_bundle(self, bundle_path):
        bundle = self._add_bundle(bundle_path)
        if bundle is not None:
            self._set_bundle_favorite(bundle.get_bundle_id(),
                                      bundle.get_activity_version(),
                                      True)
            self.emit('bundle-added', bundle)
            return True
        else:
            return False

    def _add_bundle(self, bundle_path):
        logging.debug('STARTUP: Adding bundle %r' % bundle_path)
        try:
            bundle = ActivityBundle(bundle_path)
        except MalformedBundleException:
            logging.error('Error loading bundle %r:\n%s' % (bundle_path,
                ''.join(traceback.format_exception(*sys.exc_info()))))
            return None

        if self.get_bundle(bundle.get_bundle_id()):
            return None

        self._bundles.append(bundle)
        return bundle

    def remove_bundle(self, bundle_path):
        for bundle in self._bundles:
            if bundle.get_path() == bundle_path:
                self._bundles.remove(bundle)
                self.emit('bundle-removed', bundle)
                return True
        return False

    def get_activities_for_type(self, mime_type):
        result = []
        for bundle in self._bundles:
            if bundle.get_mime_types() and mime_type in bundle.get_mime_types():
                if self.get_default_for_type(mime_type) == \
                        bundle.get_bundle_id():
                    result.insert(0, bundle)
                else:
                    result.append(bundle)
        return result

    def get_default_for_type(self, mime_type):
        if self._mime_defaults.has_key(mime_type):
            return self._mime_defaults[mime_type]
        else:
            return None

    def _find_bundle(self, bundle_id, version):
        for bundle in self._bundles:
            if bundle.get_bundle_id() == bundle_id and \
                    bundle.get_activity_version() == version:
                return bundle
        raise ValueError('No bundle %r with version %r exists.' % \
                (bundle_id, version))

    def set_bundle_favorite(self, bundle_id, version, favorite):
        changed = self._set_bundle_favorite(bundle_id, version, favorite)
        if changed:
            bundle = self._find_bundle(bundle_id, version)
            self.emit('bundle-changed', bundle)

    def _set_bundle_favorite(self, bundle_id, version, favorite):
        key = self._get_favorite_key(bundle_id, version)
        if favorite and not key in self._favorite_bundles:
            self._favorite_bundles[key] = None
        elif not favorite and key in self._favorite_bundles:
            del self._favorite_bundles[key]
        else:
            return False

        self._write_favorites_file()
        return True

    def is_bundle_favorite(self, bundle_id, version):
        key = self._get_favorite_key(bundle_id, version)
        return key in self._favorite_bundles

    def set_bundle_position(self, bundle_id, version, x, y):
        key = self._get_favorite_key(bundle_id, version)
        if key not in self._favorite_bundles:
            raise ValueError('Bundle %s %s not favorite' % (bundle_id, version))

        if self._favorite_bundles[key] is None:
            self._favorite_bundles[key] = {}
        if 'position' not in self._favorite_bundles[key] or \
                [x, y] != self._favorite_bundles[key]['position']:
            self._favorite_bundles[key]['position'] = [x, y]
        else:
            return

        self._write_favorites_file()
        bundle = self._find_bundle(bundle_id, version)
        self.emit('bundle-changed', bundle)

    def get_bundle_position(self, bundle_id, version):
        """Get the coordinates where the user wants the representation of this
        bundle to be displayed. Coordinates are relative to a 1000x1000 area.
        """
        key = self._get_favorite_key(bundle_id, version)
        if key not in self._favorite_bundles or \
                self._favorite_bundles[key] is None or \
                'position' not in self._favorite_bundles[key]:
            return (-1, -1)
        else:
            return tuple(self._favorite_bundles[key]['position'])

    def _write_favorites_file(self):
        path = env.get_profile_path('favorite_activities')
        favorites_data = {'defaults-mtime': self._last_defaults_mtime,
                          'favorites': self._favorite_bundles}
        open(path, 'w').write(cjson.encode(favorites_data))

    def is_installed(self, bundle):
        # TODO treat ContentBundle in special way
        # needs rethinking while fixing ContentBundle support
        if isinstance(bundle, ContentBundle):
            return bundle.is_installed()

        for installed_bundle in self._bundles:
            if bundle.get_bundle_id() == installed_bundle.get_bundle_id() and \
                    bundle.get_activity_version() == \
                        installed_bundle.get_activity_version():
                return True
        return False

    def install(self, bundle):
        activities_path = env.get_user_activities_path()

        if self.get_bundle(bundle.get_bundle_id()):
            raise AlreadyInstalledException

        for installed_bundle in self._bundles:
            if bundle.get_bundle_id() == installed_bundle.get_bundle_id() and \
                    bundle.get_activity_version() == \
                        installed_bundle.get_activity_version():
                raise AlreadyInstalledException
            elif bundle.get_bundle_id() == installed_bundle.get_bundle_id():
                self.uninstall(installed_bundle, force=True)

        install_dir = env.get_user_activities_path()
        install_path = bundle.install(install_dir)
        
        # TODO treat ContentBundle in special way
        # needs rethinking while fixing ContentBundle support
        if isinstance(bundle, ContentBundle):
            pass
        elif not self.add_bundle(install_path):
            raise RegistrationException

    def uninstall(self, bundle, force=False):        
        # TODO treat ContentBundle in special way
        # needs rethinking while fixing ContentBundle support
        if isinstance(bundle, ContentBundle):
            if bundle.is_installed():
                bundle.uninstall()
            else:
                logging.warning('Not uninstalling, bundle is not installed')
            return

        act = self.get_bundle(bundle.get_bundle_id())
        if not force and \
                act.get_activity_version() != bundle.get_activity_version():
            logging.warning('Not uninstalling, different bundle present')
            return
        elif not act.get_path().startswith(env.get_user_activities_path()):
            logging.warning('Not uninstalling system activity')
            return

        install_path = act.get_path()

        bundle.uninstall(install_path, force)
        
        if not self.remove_bundle(install_path):
            raise RegistrationException

    def upgrade(self, bundle):
        act = self.get_bundle(bundle.get_bundle_id())
        if act is None:
            logging.warning('Activity not installed')
        elif act.get_path().startswith(env.get_user_activities_path()):
            try:
                self.uninstall(bundle, force=True)
            except Exception:
                logging.error('Uninstall failed, still trying to install ' \
                    'newer bundle:\n' + \
                    ''.join(traceback.format_exception(*sys.exc_info())))
        else:
            logging.warning('Unable to uninstall system activity, ' \
                            'installing upgraded version in user activities')

        self.install(bundle)

_instance = None

def get_registry():
    global _instance
    if not _instance:
        _instance = BundleRegistry()
    return _instance
