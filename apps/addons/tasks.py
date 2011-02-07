import os
import logging

from django.db import connection, transaction

from celeryutils import task

import amo
from amo.decorators import write
from . import cron  # Pull in tasks from cron.
from .models import Addon, Preview, index

log = logging.getLogger('z.task')


@task
@write
def version_changed(addon_id, **kw):
    update_last_updated(addon_id)
    update_appsupport([addon_id])


def update_last_updated(addon_id):
    log.info('[1@None] Updating last updated for %s.' % addon_id)
    queries = Addon._last_updated_queries()
    addon = Addon.objects.get(pk=addon_id)
    if addon.is_persona():
        q = 'personas'
    elif addon.status == amo.STATUS_PUBLIC:
        q = 'public'
    elif addon.status == amo.STATUS_LISTED:
        q = 'listed'
    else:
        q = 'exp'
    pk, t = queries[q].filter(pk=addon_id).values_list('id', 'last_updated')[0]
    Addon.objects.filter(pk=pk).update(last_updated=t)


@transaction.commit_manually
def update_appsupport(ids):
    log.info("[%s@None] Updating appsupport for %s." % (len(ids), ids))
    delete = 'DELETE FROM appsupport WHERE addon_id IN (%s)'
    insert = """INSERT INTO appsupport (addon_id, app_id, created, modified)
                VALUES %s"""

    addons = Addon.uncached.filter(id__in=ids).no_transforms()
    apps = [(addon.id, app.id) for addon in addons
            for app in addon.compatible_apps]
    s = ','.join('(%s, %s, NOW(), NOW())' % x for x in apps)

    if not apps:
        return

    cursor = connection.cursor()
    cursor.execute(delete % ','.join(map(str, ids)))
    cursor.execute(insert % s)
    transaction.commit()

    # All our updates were sql, so invalidate manually.
    Addon.objects.invalidate(*addons)


@task
def delete_preview_files(id, **kw):
    log.info('[1@None] Removing preview with id of %s.' % id)

    p = Preview(id=id)
    for f in (p.thumbnail_path, p.image_path):
        try:
            os.remove(f)
        except Exception, e:
            log.error('Error deleting preview file (%s): %s' % (f, e))


@task
def reindex(*ids, **kw):
    index(ids)
