# coding: utf-8
# Author: Toshio Kuratomi <tkuratom@redhat.com>
# License: GPLv3+
# Copyright: Ansible Project, 2020

import asyncio
import os
import os.path
import pkgutil
import shutil
import tempfile
from functools import partial

import aiofiles
import aiohttp
import sh
from jinja2 import Template

from .dependency_files import BuildFile, DepsFile
from .galaxy import CollectionDownloader


#
# Common code
#

class CollectionFormatError(Exception):
    pass


async def download_collections(deps, download_dir):
    requestors = {}
    async with aiohttp.ClientSession() as aio_session:
        for collection_name, version_spec in deps.items():
            downloader = CollectionDownloader(aio_session, download_dir)
            requestors[collection_name] = asyncio.create_task(
                downloader.retrieve(collection_name, version_spec, download_dir))

        included_versions = {}
        responses = await asyncio.gather(*requestors.values())
        for idx, collection_name in enumerate(requestors):
            included_versions[collection_name] = responses[idx]

    return included_versions


#
# Single sdist for ansible
#

async def install_collections_together(version, tmp_dir):
    loop = asyncio.get_running_loop()
    package_dir = os.path.join(tmp_dir, f'ansible-{version}')
    os.mkdir(package_dir, mode=0o700)
    ansible_collections_dir = os.path.join(package_dir, 'ansible_collections')
    os.mkdir(ansible_collections_dir, mode=0o700)

    installers = []
    collection_tarballs = ((p, f) for f in os.listdir(tmp_dir)
                           if os.path.isfile(p := os.path.join(tmp_dir, f)))
    for pathname, filename in collection_tarballs:
        namespace, collection, _dummy = filename.split('-', 2)
        collection_dir = os.path.join(ansible_collections_dir, namespace, collection)
        # Note: this is okay because we created package_dir ourselves as a directory
        # that only we can access
        os.makedirs(collection_dir, mode=0o700, exist_ok=False)

        # If the choice of install tools for galaxy is ever settled upon, we can switch from tar to
        # using that
        installers.append(loop.run_in_executor(None, sh.tar, '-xf', pathname, '-C', collection_dir))

    await asyncio.gather(*installers)


def copy_boilerplate_files(package_dir):
    gpl_license = pkgutil.get_data('ansible_infra.data', 'gplv3.txt')
    with open(os.path.join(package_dir, 'COPYING'), 'wb') as f:
        f.write(gpl_license)

    readme = pkgutil.get_data('ansible_infra.data', 'acd-readme.txt')
    with open(os.path.join(package_dir, 'README'), 'wb') as f:
        f.write(readme)


def write_manifest(package_dir):
    manifest_file = os.path.join(package_dir, 'MANIFEST.in')
    with open(manifest_file, 'w') as f:
        f.write('include COPYING\n')
        f.write('include README\n')
        f.write('recursive-include ansible_collections/ **\n')


def write_setup(package_dir, acd_version):
    setup_filename = os.path.join(package_dir, 'setup.py')

    setup_tmpl = Template(pkgutil.get_data('ansible_infra.data', 'acd-setup_py.j2').decode('utf-8'))
    setup_contents = setup_tmpl.render(version=acd_version, collection_deps='')

    with open(setup_filename, 'w') as f:
        f.write(setup_contents)


def write_python_build_files(acd_version, dest_dir):
    toplevel_dir = os.path.join(dest_dir, f'ansible-{acd_version}')

    copy_boilerplate_files(toplevel_dir)
    write_manifest(toplevel_dir)
    write_setup(toplevel_dir, acd_version)


def make_dist(ansible_dir, dest_dir):
    sh.python('setup.py', 'sdist', _cwd=ansible_dir)
    dist_dir = os.path.join(ansible_dir, 'dist')
    files = os.listdir(dist_dir)
    if len(files) != 1:
        raise Exception('python setup.py sdist should only have created one file')

    shutil.move(os.path.join(dist_dir, files[0]), dest_dir)


def build_single_command(args):
    build_file = BuildFile(args.build_file)
    build_acd_version, ansible_base_version, deps = build_file.parse()

    if not str(args.acd_version).startswith(build_acd_version):
        print(f'{args.build_file} is for version {build_acd_version} but we need'
              ' {args.acd_version.major}.{arg.acd_version.minor}')

    with tempfile.TemporaryDirectory() as download_dir:
        included_versions = asyncio.run(download_collections(deps, download_dir))
        asyncio.run(install_collections_together(args.acd_version, download_dir))
        write_python_build_files(args.acd_version, download_dir)
        make_dist(os.path.join(download_dir, f'ansible-{args.acd_version}'), args.dest_dir)

    deps_filename = os.path.join(args.dest_dir, args.deps_file)
    deps_file = DepsFile(deps_filename)
    deps_file.write(args.acd_version, ansible_base_version, included_versions)

    return 0

#
# Code to make one sdist per collection
#


async def install_collections_separately(version, tmp_dir):
    loop = asyncio.get_running_loop()
    collection_tarballs = ((p, f) for f in os.listdir(tmp_dir)
                           if os.path.isfile(p := os.path.join(tmp_dir, f)))

    installers = []
    collection_dirs = []
    for pathname, filename in collection_tarballs:
        namespace, collection, version_ext = filename.split('-', 2)
        for ext in ('.tar.gz',):
            # Note: If galaxy allows other archive formats, add their extensions here
            ext_start = version_ext.find(ext)
            if ext_start != -1:
                version = version_ext[:ext_start]
                break
        else:
            raise CollectionFormatError('Collection filename was in an unexpected'
                                        f' format: {filename}')

        package_dir = os.path.join(tmp_dir, f'ansible-collections-{namespace}.'
                                   f'{collection}-{version}')
        os.mkdir(package_dir, mode=0o700)
        ansible_collections_dir = os.path.join(package_dir, 'ansible_collections')
        os.mkdir(ansible_collections_dir, mode=0o700)

        collection_dirs.append(os.path.join(ansible_collections_dir, namespace, collection))
        # Note: this is okay because we created package_dir ourselves as a directory
        # that only we can access
        os.makedirs(collection_dirs[-1], mode=0o700, exist_ok=False)

        # If the choice of install tools for galaxy is ever settled upon, we can switch from tar to
        # using that
        installers.append(loop.run_in_executor(None, sh.tar, '-xf', pathname,
                                               '-C', collection_dirs[-1]))

    await asyncio.gather(*installers)

    return collection_dirs


async def write_collection_readme(package_dir, collection_name):
    readme_tmpl = Template(pkgutil.get_data('ansible_infra.data',
                                            'collection-readme.j2').decode('utf-8'))
    readme_contents = setup_tmpl.render(collection_name=collection_name)

    readme_filename = os.path.join(package_dir, 'README.rst')
    async with aiofiles.open(readme_filename, 'w') as f:
        await f.write(readme_contents)


async def write_collection_setup(package_dir, name, version):
    setup_filename = os.path.join(package_dir, 'setup.py')

    setup_tmpl = Template(pkgutil.get_data('ansible_infra.data',
                                           'collection-setup_py.j2').decode('utf-8'))
    setup_contents = setup_tmpl.render(version=version, name=name)

    with open(setup_filename, 'w') as f:
        await f.write(setup_contents)


async def write_collection_manifest(package_dir):
    manifest_file = os.path.join(package_dir, 'MANIFEST.in')
    async with aiofiles.open(manifest_file, 'w') as f:
        await f.write('include README.rst\n')
        await f.write('recursive-include ansible_collections/ **\n')


async def make_collection_dist(name, version, package_dir, dest_dir):
    # Copy boilerplate into place
    await write_collection_readme(name, package_dir)
    await write_collection_setup(package_dir, name, version)
    await write_collection_manifest(package_dir)

    loop = asyncio.get_running_loop()

    # Create the python sdist
    await loop.run_in_executor(None, partial(sh.python, 'setup.py', 'sdist', _cwd=package_dir))
    dist_dir = os.path.join(package_dir, 'dist')
    files = os.listdir(dist_dir)
    if len(files) != 1:
        raise Exception('python setup.py sdist should only have created one file')

    dist_file = os.path.join(dist_dir, files[0])
    await loop.run_in_executor(shutil.move, dist_file, dest_dir)


async def make_collection_dists(dest_dir, collection_dirs):
    dist_creators = []
    for collection_dir in collection_dirs:
        dir_name_only = os.path.basename(collection_dir)
        dummy_, name, version = dir_name_only.rsplit('-', 2)

        dist_creators.append(asyncio.create_task(
            make_collection_dist(name, version, collection_dir, dest_dir)))

    await asyncio.gather(*dist_creators)


def build_multiple_command(args):
    build_file = BuildFile(args.build_file)
    build_acd_version, ansible_base_version, deps = build_file.parse()

    if not str(args.acd_version).startswith(build_acd_version):
        print(f'{args.build_file} is for version {build_acd_version} but we need'
              ' {args.acd_version.major}.{arg.acd_version.minor}')

    with tempfile.TemporaryDirectory() as download_dir:
        included_versions = asyncio.run(download_collections(deps, download_dir))
        collection_dirs = asyncio.run(install_collections_separately(args.acd_version,
                                                                     download_dir))
        asyncio.run(make_collection_dists(args.dest_dir, collection_dirs))

    ### FIXME: We are here:
    # Create the ansible package that deps on the collections we just wrote

    # Write the deps file
    deps_filename = os.path.join(args.dest_dir, args.deps_file)
    deps_file = DepsFile(deps_filename)
    deps_file.write(args.acd_version, ansible_base_version, included_versions)

    return 0
