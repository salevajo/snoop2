import json
from pathlib import Path
from django.utils import timezone
from datetime import datetime
from . import models
from .tasks import shaorma, extract_text
from .tasks import SEVENZIP_KNOWN_TYPES, unarchive


def time_from_unix(t):
    return timezone.utc.fromutc(datetime.utcfromtimestamp(t))


def directory_absolute_path(directory):
    path_elements = []
    node = directory
    path = Path(directory.collection.root)

    while node.parent_directory:
        path_elements.append(node.name)
        node = node.parent_directory
    for name in reversed(path_elements):
        path /= name

    return path


@shaorma
def walk(directory_pk):
    directory = models.Directory.objects.get(pk=directory_pk)
    path = directory_absolute_path(directory)
    for thing in path.iterdir():
        if thing.is_dir():
            (child_directory, _) = directory.child_directory_set.get_or_create(
                collection=directory.collection,
                name=thing.name,
            )
            walk.laterz(child_directory.pk)
        else:
            file_to_blob.laterz(directory_pk, thing.name)


@shaorma
def file_to_blob(directory_pk, name):
    directory = models.Directory.objects.get(pk=directory_pk)
    path = directory_absolute_path(directory) / name
    blob = models.Blob.create_from_file(path)

    stat = path.stat()
    file, _ = directory.child_file_set.get_or_create(
        name=name,
        defaults=dict(
            collection=directory.collection,
            ctime=time_from_unix(stat.st_ctime),
            mtime=time_from_unix(stat.st_mtime),
            size=stat.st_size,
            blob=blob,
        ),
    )

    handle_file.laterz(file.pk)


@shaorma
def handle_file(file_pk):
    file = models.File.objects.get(pk=file_pk)
    blob = file.blob
    depends_on = {}

    if blob.mime_type in SEVENZIP_KNOWN_TYPES:
        unarchive_task = unarchive.laterz(blob.pk)
        create_archive_files.laterz(
            file.pk,
            depends_on={'archive_listing': unarchive_task},
        )

    if blob.mime_type == 'text/plain':
        depends_on['text'] = extract_text.laterz(blob.pk)

    digest.laterz(file.collection.pk, blob.pk, depends_on=depends_on)


@shaorma
def create_archive_files(file_pk, archive_listing):
    with archive_listing.open() as f:
        archive_listing_data = json.load(f)

    def create_directory_children(directory, children):
        for item in children:
            if item['type'] == 'file':
                blob = models.Blob.objects.get(pk=item['blob_pk'])
                create_file(directory, item['name'], blob)

            if item['type'] == 'directory':
                create_directory(directory, item['name'], item['children'])

    def create_directory(parent_directory, name, children):
        (directory, _) = parent_directory.child_directory_set.get_or_create(
            name=name,
            defaults=dict(
                collection=parent_directory.collection,
            ),
        )
        create_directory_children(directory, children)

    def create_file(parent_directory, name, blob):
        size = blob.path().stat().st_size
        now = timezone.now()

        parent_directory.child_file_set.get_or_create(
            name=name,
            defaults=dict(
                collection=parent_directory.collection,
                ctime=now,
                mtime=now,
                size=size,
                blob=blob,
            ),
        )

    file = models.File.objects.get(pk=file_pk)
    (fake_root, _) = file.child_directory_set.get_or_create(
        name='',
        defaults=dict(
            collection=file.collection,
        ),
    )
    create_directory_children(fake_root, archive_listing_data)


@shaorma
def digest(collection_pk, blob_pk, **depends_on):
    collection = models.Collection.objects.get(pk=collection_pk)
    blob = models.Blob.objects.get(pk=blob_pk)

    rv = {}
    text_blob = depends_on.get('text')
    if text_blob:
        with text_blob.open() as f:
            text_bytes = f.read()
        rv['text'] = text_bytes.decode(text_blob.mime_encoding)

    with models.Blob.create() as writer:
        writer.write(json.dumps(rv).encode('utf-8'))

    collection.digest_set.get_or_create(blob=blob, result=writer.blob)
