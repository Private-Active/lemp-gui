# -*- coding: utf-8 -*-
#
# Copyright (c) 2017, doudoudzj
# Copyright (c) 2012 - 2016, VPSMate development team
# All rights reserved.
#
# InPanel is distributed under the terms of the (new) BSD License.
# The full license can be found in 'LICENSE'.

'''Module for file Management'''

import anydbm
import re
from grp import getgrgid, getgrnam
from mimetypes import guess_type
from os import chmod as oschmod
from os import chown as oschown
from os import listdir as oslistdir
from os import lstat as oslstat
from os import mkdir, readlink
from os import rename as osrename
from os import stat as osstat
from os import symlink, walk
from os.path import abspath, basename, dirname, exists, isdir, islink, join
from pwd import getpwnam, getpwuid
from stat import *
from subprocess import PIPE, Popen
from time import time
from uuid import uuid4

import lib.magic
from core.utils import b2h, ftime
from .server import ServerInfo

charsets = ('utf-8', 'gb2312', 'gbk', 'gb18030', 'big5', 'euc-jp', 'euc-kr', 'iso-8859-2', 'shift_jis')


def listdir(path, showdotfiles=False, onlydir=None):
    path = abspath(path)
    if not exists(path) or not isdir(path):
        return False
    items = sorted(oslistdir(path))
    if not showdotfiles:
        items = [item for item in items if not item.startswith('.')]
    for i, item in enumerate(items):
        items[i] = getitem(join(path, item))
    # let folders list before files
    rt = []
    for i in xrange(len(items)-1, -1, -1):
        if items[i]['isdir'] or items[i]['islnk'] and not items[i]['link_broken'] and items[i]['link_isdir']:
            rt.insert(0, items.pop(i))
    # check if only list directories
    if not onlydir:
        rt.extend(items)
    return rt


def listfile(directory):
    '''only list files of directory'''
    d = abspath(directory)
    if not exists(d) or not isdir(d):
        return None
    items = sorted(oslistdir(d))
    return items if len(items) > 0 else []


def getitem(path):
    if not exists(path) and not islink(path):
        return False
    name = basename(path)
    basepath = dirname(path)
    stat = oslstat(path)
    mode = stat.st_mode
    try:
        uname = getpwuid(stat.st_uid).pw_name
    except:
        uname = ''
    try:
        gname = getgrgid(stat.st_gid).gr_name
    except:
        gname = ''
    item = {
        'name': name,
        'isdir': S_ISDIR(mode),
        # 'ischr': S_ISCHR(mode),
        # 'isblk': S_ISBLK(mode),
        'isreg': S_ISREG(mode),
        # 'isfifo': S_ISFIFO(mode),
        'islnk': S_ISLNK(mode),
        # 'issock': S_ISSOCK(mode),
        'perms': oct(stat.st_mode & 0777),
        'uid': stat.st_uid,
        'gid': stat.st_gid,
        'uname': uname,
        'gname': gname,
        'size': b2h(stat.st_size),
        'atime': ftime(stat.st_atime),
        'mtime': ftime(stat.st_mtime),
        'ctime': ftime(stat.st_ctime),
    }
    if item['islnk']:
        linkfile = readlink(path)
        item['linkto'] = linkfile
        if not linkfile.startswith('/'):
            linkfile = abspath(join(basepath, linkfile))
        try:
            stat = osstat(linkfile)
            item['link_isdir'] = S_ISDIR(stat.st_mode)
            item['link_isreg'] = S_ISREG(stat.st_mode)
            item['link_broken'] = False
        except:
            item['link_broken'] = True
    return item


def rename(oldpath, newname):
    # path = abspath(oldpath)
    if not exists(oldpath):
        return False
    try:
        basepath = dirname(oldpath)
        newpath = join(basepath, newname)
        osrename(oldpath, newpath)
        return True
    except:
        return False


def link(srcpath, despath):
    try:
        symlink(srcpath, despath)
        return True
    except:
        return False


def dadd(path, name):
    path = abspath(path)
    if not exists(path) or not isdir(path):
        return False
    dpath = join(path, name)
    if exists(dpath):
        return False
    try:
        mkdir(dpath)
        return True
    except:
        return False


def istext(path):
    if not exists('/usr/file') and not exists('/usr/bin/file'):
        return True
    return (re.search(r':(.* text|.* empty)', Popen(["file", '-L', path], stdout=PIPE).stdout.read()) is not None)


def mimetype(filepath):
    if not exists(filepath):
        return False
    if islink(filepath):
        linkfile = readlink(filepath)
        if linkfile.startswith('/'):
            filepath = linkfile
        else:
            basepath = dirname(filepath)
            filepath = abspath(join(basepath, linkfile))
        if not exists(filepath):
            return False
    mime = lib.magic.from_file(filepath, mime=True)
    # sometimes it still return like "text/plain; charset=us-ascii"
    if ';' in mime:
        mime = mime.split(';', 1)[0]
    if mime == 'text/plain':
        tmime = guess_type(filepath)[0]
        if tmime:
            mime = tmime
    return mime


def fsize(filepath):
    if not exists(filepath):
        return None
    return oslstat(filepath).st_size


def fadd(path, name):
    path = abspath(path)
    if not exists(path) or not isdir(path):
        return False
    fpath = join(path, name)
    if exists(fpath):
        return False
    try:
        with open(fpath, 'w'):
            pass
        return True
    except:
        return False


def fsave(path, content, bakup=True):
    if not exists(path):
        return False
    try:
        if bakup:
            dname = dirname(path)
            filename = '.%s.bak' % basename(path)
            osrename(path, join(dname, filename))
        with open(path, 'w') as f:
            f.write(content)
        return True
    except:
        return False


def decode(content):
    """Detect charset of content and decode it.
    """
    for charset in charsets:
        try:
            content = content.decode(charset)
            return (charset, content)
        except:
            pass
    return (None, content)


def encode(content, charset):
    """Encode content using specified charset.
    """
    try:
        return content.encode(charset)
    except:
        return False


def delete(path):
    if not exists(path) and not islink(path):
        return False
    path = abspath(path)
    mounts = _getmounts()
    mount = ''
    for m in mounts:
        if path.startswith(m):
            mount = m
            break
    if not mount:
        return False
    trashpath = join(mount, '.deleted_files')
    _inittrash(mounts)
    try:
        uuid = str(uuid4())
        filename = basename(path)
        db = anydbm.open(join(trashpath, '.fileinfo'), 'c')
        db[uuid] = '\t'.join([filename, path, str(int(time()))])

        osrename(path, join(trashpath, uuid))
        # deal with the .filename.bak
        dname = dirname(path)
        bakfilepath = join(dname, '.%s.bak' % filename)
        if exists(bakfilepath):
            return delete(bakfilepath)
        return True
    except:
        return False
    finally:
        db.close()


def _getmounts():
    mounts = ServerInfo.mounts()
    mounts = [mount['path'] for mount in mounts]
    # let the longest path at the first
    mounts.sort(lambda x, y: cmp(len(y), len(x)))
    return mounts


def _inittrash(mounts=None):
    # initialize the trash
    if not mounts:
        mounts = _getmounts()
    for mount in mounts:
        trashpath = join(mount, '.deleted_files')
        if not exists(trashpath):
            mkdir(trashpath)
            metafile = join(trashpath, '.fileinfo')
            anydbm.open(metafile, 'c').close()


def trashs():
    """Return trash path list.
    """
    mounts = _getmounts()
    return [join(mount, '.deleted_files') for mount in mounts]


def tlist():
    mounts = _getmounts()
    _inittrash(mounts)
    # gather informations in each mount point's trash
    items = []
    for mount in mounts:
        db = anydbm.open(join(mount, '.deleted_files', '.fileinfo'), 'c')
        for uuid, info in db.items():
            fields = info.split('\t')
            item = {
                'uuid': uuid,
                'name': fields[0],
                'path': fields[1],
                'time': ftime(float(fields[2])),
                'mount': mount
            }
            filepath = join(mount, '.deleted_files', uuid)
            if exists(filepath):
                stat = osstat(filepath)
                item['isdir'] = S_ISDIR(stat.st_mode)
                item['isreg'] = S_ISREG(stat.st_mode)
                item['islnk'] = S_ISLNK(stat.st_mode)
            items.append(item)
        db.close()
    items.sort(lambda x, y: cmp(y['time'], x['time']))
    return items


def titem(mount, uuid):
    # _inittrash()
    try:
        trashpath = join(mount, '.deleted_files')
        db = anydbm.open(join(trashpath, '.fileinfo'), 'c')
        info = db[uuid]
        db.close()
        fields = info.split('\t')
        info = {
            'uuid': uuid,
            'name': fields[0],
            'path': fields[1],
            'time': ftime(float(fields[2])),
            'mount': mount
        }
        info['originpath'] = join(trashpath, uuid)
        return info
    except:
        return False


def trestore(mount, uuid):
    # _inittrash()
    try:
        info = titem(mount, uuid)
        trashpath = join(mount, '.deleted_files')
        osrename(join(trashpath, uuid), info['path'])
        db = anydbm.open(join(trashpath, '.fileinfo'), 'c')
        del db[uuid]
        db.close()
        return True
    except:
        return False


def tdelete(mount, uuid):
    # the real file or directory should be deleted external
    # _inittrash()
    try:
        db = anydbm.open(join(
            mount, '.deleted_files', '.fileinfo'), 'c')
        del db[uuid]
        db.close()
        return True
    except:
        return False


def chown(path, user, group, recursively=False):
    if not exists(path) and not islink(path):
        return False
    try:
        userid = groupid = -1
        if user:
            userid = getpwnam(user).pw_uid
        if group:
            groupid = getgrnam(group).gr_gid
        if isdir(path) and recursively:
            for root, dirs, files in walk(path):
                for momo in dirs:
                    tpath = join(root, momo)
                    if not exists(tpath):
                        continue  # maybe broken link
                    oschown(tpath, userid, groupid)
                for momo in files:
                    tpath = join(root, momo)
                    if not exists(tpath):
                        continue
                    oschown(tpath, userid, groupid)
        oschown(path, userid, groupid)
    except:
        return False
    return True


def chmod(path, perms, recursively=False):
    if not exists(path) and not islink(path):
        return False
    try:
        if isdir(path) and recursively:
            for root, dirs, files in walk(path):
                for momo in dirs:
                    tpath = join(root, momo)
                    if not exists(tpath):
                        continue  # maybe broken link
                    oschmod(tpath, perms)
                for momo in files:
                    tpath = join(root, momo)
                    if not exists(tpath):
                        continue
                    oschmod(tpath, perms)
        oschmod(path, perms)
    except:
        return False
    return True



if __name__ == '__main__':
    print('* List directory of /root:')
    for item in listdir('/root'):
        print('  name: %s' % item['name'])
        print('  isdir: %s' % str(item['isdir']))
        print('  isreg: %s' % str(item['isreg']))
        print('  islnk: %s' % str(item['islnk']))
        print('  perms: %s' % str(item['perms']))
        print('  uname: %s' % item['uname'])
        print('  gname: %s' % item['gname'])
        print('  size: %s' % item['size'])
        print('  atime: %s' % item['atime'])
        print('  mtime: %s' % item['mtime'])
        print('  ctime: %s' % item['ctime'])
        print('  istext: %s' % str(istext(join('/root', item['name']))))
        print('  mimetype: %s' % mimetype(join('/root', item['name'])))
        print
    print
