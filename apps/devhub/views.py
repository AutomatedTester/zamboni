import codecs
import collections
import functools
import json
import os
import path
import sys
import time
import traceback
import uuid
import operator

from django import http
from django.conf import settings
from django import forms as django_forms
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import urlquote
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_view_exempt

import bleach
import commonware.log
import jingo
import jinja2
from tower import ugettext_lazy as _lazy, ugettext as _
from session_csrf import anonymous_csrf

from applications.models import AppVersion
import amo
import amo.utils
from amo import messages, urlresolvers
from amo.helpers import urlparams
from amo.utils import MenuItem
from amo.urlresolvers import reverse
from amo.decorators import json_view, login_required, post_required
from access import acl
from addons import forms as addon_forms
from addons.decorators import addon_view
from addons.models import Addon, AddonUser
from addons.views import BaseFilter
from devhub.models import ActivityLog, BlogPost, RssKey, SubmitStep
from editors.helpers import get_position
from files.models import File, FileUpload
from files.utils import parse_addon
from translations.models import delete_translation
from users.models import UserProfile
from versions.models import License, Version
from product_details import get_regions
from zadmin.models import ValidationResult

from . import forms, tasks, feeds, responsys, signals

log = commonware.log.getLogger('z.devhub')


# We use a session cookie to make sure people see the dev agreement.
DEV_AGREEMENT_COOKIE = 'yes-I-read-the-dev-agreement'


def dev_required(owner_for_post=False, allow_editors=False):
    """Requires user to be add-on owner or admin.

    When allow_editors is True, an editor can view the page.
    """
    def decorator(f):
        @addon_view
        @login_required
        @functools.wraps(f)
        def wrapper(request, addon, *args, **kw):
            fun = lambda: f(request, addon_id=addon.id, addon=addon, *args,
                            **kw)
            if allow_editors:
                if acl.action_allowed(request, 'Editors', '%'):
                    return fun()
            # Require an owner or dev for POST requests.
            if request.method == 'POST':
                if acl.check_addon_ownership(request, addon,
                                             dev=not owner_for_post):
                    return fun()
            # Ignore disabled so they can view their add-on.
            elif acl.check_addon_ownership(request, addon, viewer=True,
                                           ignore_disabled=True):
                step = SubmitStep.objects.filter(addon=addon)
                # Redirect to the submit flow if they're not done.
                if not getattr(f, 'submitting', False) and step:
                    return _resume(addon, step)
                return fun()
            return http.HttpResponseForbidden()
        return wrapper
    # The arg will be a function if they didn't pass owner_for_post.
    if callable(owner_for_post):
        f = owner_for_post
        owner_for_post = False
        return decorator(f)
    else:
        return decorator


class AddonFilter(BaseFilter):
    opts = (('name', _lazy(u'Name')),
            ('updated', _lazy(u'Updated')),
            ('created', _lazy(u'Created')),
            ('popular', _lazy(u'Downloads')),
            ('rating', _lazy(u'Rating')))


def addon_listing(request, addon_type, default='name'):
    """Set up the queryset and filtering for addon listing for Dashboard."""
    qs = request.amo_user.addons.all()
    filter = AddonFilter(request, qs, 'sort', default)
    return filter.qs, filter


def index(request):
    posts = _get_posts()
    return jingo.render(request, 'devhub/index.html', {'blog_posts': posts})


@login_required
def dashboard(request):
    addons, filter = addon_listing(request, addon_type=amo.ADDON_ANY)
    addons = amo.utils.paginate(request, addons, per_page=10)
    blog_posts = _get_posts()
    data = dict(addons=addons, sorting=filter.field,
                items=_get_items(None, request.amo_user.addons.all())[:4],
                sort_opts=filter.opts, rss=_get_rss_feed(request),
                blog_posts=blog_posts, timestamp=int(time.time()))
    return jingo.render(request, 'devhub/addons/dashboard.html', data)


@dev_required
def ajax_compat_status(request, addon_id, addon):
    if not (addon.accepts_compatible_apps() and addon.current_version):
        raise http.Http404()
    return jingo.render(request, 'devhub/addons/ajax_compat_status.html',
                        dict(addon=addon))


@dev_required
def ajax_compat_error(request, addon_id, addon):
    if not (addon.accepts_compatible_apps() and addon.current_version):
        raise http.Http404()
    return jingo.render(request, 'devhub/addons/ajax_compat_error.html',
                        dict(addon=addon))


@dev_required
def ajax_compat_update(request, addon_id, addon, version_id):
    if not addon.accepts_compatible_apps():
        raise http.Http404()
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    compat_form = forms.CompatFormSet(request.POST or None,
                                      queryset=version.apps.all())
    if request.method == 'POST' and compat_form.is_valid():
        for compat in compat_form.save(commit=False):
            compat.version = version
            compat.save()
    return jingo.render(request, 'devhub/addons/ajax_compat_update.html',
                        dict(addon=addon, version=version,
                             compat_form=compat_form))


def _get_addons(request, addons, addon_id, action):
    """Create a list of ``MenuItem``s for the activity feed."""
    items = []

    a = MenuItem()
    a.selected = (not addon_id)
    (a.text, a.url) = (_('All My Add-ons'), reverse('devhub.feed_all'))
    if action:
        a.url += '?action=' + action
    items.append(a)

    for addon in addons:
        item = MenuItem()
        try:
            item.selected = (addon_id and addon.id == int(addon_id))
        except ValueError:
            pass  # We won't get here... EVER
        url = reverse('devhub.feed', args=[addon.slug])
        if action:
            url += '?action=' + action
        item.text, item.url = addon.name, url
        items.append(item)

    return items


def _get_posts(limit=5):
    return BlogPost.objects.order_by('-date_posted')[0:limit]


def _get_activities(request, action):
    url = request.get_full_path()
    choices = (None, 'updates', 'status', 'collections', 'reviews')
    text = {None: _('All Activity'),
            'updates': _('Add-on Updates'),
            'status': _('Add-on Status'),
            'collections': _('User Collections'),
            'reviews': _('User Reviews'),
            }

    items = []
    for c in choices:
        i = MenuItem()
        i.text = text[c]
        i.url, i.selected = urlparams(url, page=None, action=c), (action == c)
        items.append(i)

    return items


def _get_items(action, addons):
    filters = dict(updates=(amo.LOG.ADD_VERSION, amo.LOG.ADD_FILE_TO_VERSION),
                   status=(amo.LOG.USER_DISABLE, amo.LOG.USER_ENABLE,
                           amo.LOG.CHANGE_STATUS, amo.LOG.APPROVE_VERSION,),
                   collections=(amo.LOG.ADD_TO_COLLECTION,
                            amo.LOG.REMOVE_FROM_COLLECTION,),
                   reviews=(amo.LOG.ADD_REVIEW,))

    filter = filters.get(action)
    items = ActivityLog.objects.for_addons(addons)
    if filter:
        items = items.filter(action__in=[i.id for i in filter])

    return items


def _get_rss_feed(request):
    key, __ = RssKey.objects.get_or_create(user=request.amo_user)
    return urlparams(reverse('devhub.feed_all'), privaterss=key.key)


def feed(request, addon_id=None):
    if request.GET.get('privaterss'):
        return feeds.ActivityFeedRSS()(request)

    addon_selected = None

    if not request.user.is_authenticated():
        url = reverse('users.login')
        p = urlquote(request.get_full_path())
        return http.HttpResponseRedirect('%s?to=%s' % (url, p))
    else:
        addons_all = request.amo_user.addons.all()

        if addon_id:
            addon = get_object_or_404(Addon.objects.id_or_slug(addon_id))
            addons = addon  # common query set
            try:
                key = RssKey.objects.get(addon=addons)
            except RssKey.DoesNotExist:
                key = RssKey.objects.create(addon=addons)

            addon_selected = addon.id

            rssurl = urlparams(reverse('devhub.feed', args=[addon_id]),
                               privaterss=key.key)

            if not acl.check_addon_ownership(request, addons, viewer=True,
                                             ignore_disabled=True):
                return http.HttpResponseForbidden()
        else:
            rssurl = _get_rss_feed(request)
            addon = None
            addons = addons_all

    action = request.GET.get('action')

    items = _get_items(action, addons)

    activities = _get_activities(request, action)
    addon_items = _get_addons(request, addons_all, addon_selected, action)

    pager = amo.utils.paginate(request, items, 20)
    data = dict(addons=addon_items, pager=pager, activities=activities,
                rss=rssurl, addon=addon)
    return jingo.render(request, 'devhub/addons/activity.html', data)


@dev_required
def edit(request, addon_id, addon):

    data = {
       'page': 'edit',
       'addon': addon,
       'valid_slug': addon.slug,
       'tags': addon.tags.not_blacklisted().values_list('tag_text', flat=True),
       'previews': addon.previews.all()}

    if acl.action_allowed(request, 'Admin', 'ConfigureAnyAddon'):
        data['admin_form'] = forms.AdminForm(instance=addon)

    return jingo.render(request, 'devhub/addons/edit.html', data)


@dev_required(owner_for_post=True)
def delete(request, addon_id, addon):
    form = forms.DeleteForm(request)
    if form.is_valid():
        addon.delete('Removed via devhub')
        messages.success(request, _('Add-on deleted.'))
        return redirect('devhub.addons')
    else:
        messages.error(request,
                       _('Password was incorrect.  Add-on was not deleted.'))
        return redirect('devhub.versions', addon.slug)


@dev_required
def enable(request, addon_id, addon):
    addon.update(disabled_by_user=False)
    amo.log(amo.LOG.USER_ENABLE, addon)
    return redirect('devhub.versions', addon.slug)


@dev_required(owner_for_post=True)
def cancel(request, addon_id, addon):
    if addon.status in amo.STATUS_UNDER_REVIEW:
        if addon.status == amo.STATUS_LITE_AND_NOMINATED:
            addon.update(status=amo.STATUS_LITE)
        else:
            addon.update(status=amo.STATUS_NULL)
        amo.log(amo.LOG.CHANGE_STATUS, addon.get_status_display(), addon)
    return redirect('devhub.versions', addon.slug)


@dev_required
@post_required
def disable(request, addon_id, addon):
    addon.update(disabled_by_user=True)
    amo.log(amo.LOG.USER_DISABLE, addon)
    return redirect('devhub.versions', addon.slug)


def _license_form(request, addon, save=False, log=True):
    qs = addon.versions.order_by('-version')[:1]
    version = qs[0] if qs else None
    if version:
        instance, initial = version.license, None
        # Clear out initial data if it's a builtin license.
        if getattr(instance, 'builtin', None):
            instance, initial = None, {'builtin': instance.builtin}
        license_form = forms.LicenseForm(request.POST or None, initial=initial,
                                         instance=instance)

    if save and version and license_form.is_valid():
        changed = license_form.changed_data
        license = license_form.save()
        if changed or license != version.license:
            version.update(license=license)
            if log:
                amo.log(amo.LOG.CHANGE_LICENSE, license, addon)

    license_urls = dict(License.objects.builtins()
                        .values_list('builtin', 'url'))
    return dict(license_urls=license_urls, version=version,
                license_form=version and license_form,
                license_other_val=License.OTHER)


def _policy_form(request, addon, save=False):
    policy_form = forms.PolicyForm(
        request.POST or None, instance=addon,
        initial=dict(has_priv=bool(addon.privacy_policy),
                     has_eula=bool(addon.eula)))
    if save and policy_form.is_valid():
        policy_form.save()
        if 'privacy_policy' in policy_form.changed_data:
            amo.log(amo.LOG.CHANGE_POLICY, addon, policy_form.instance)
    return policy_form


@dev_required(owner_for_post=True)
def ownership(request, addon_id, addon):
    fs, ctx = [], {}
    # Authors.
    qs = AddonUser.objects.filter(addon=addon).order_by('position')
    user_form = forms.AuthorFormSet(request.POST or None, queryset=qs)
    fs.append(user_form)
    # Versions.
    ctx.update(_license_form(request, addon))
    if ctx['license_form']:
        fs.append(ctx['license_form'])
    # Policy.
    policy_form = _policy_form(request, addon)
    fs.append(policy_form)

    if request.method == 'POST' and all([form.is_valid() for form in fs]):
        # Authors.
        authors = user_form.save(commit=False)
        for author in authors:
            action = None
            if not author.id or author.user_id != author._original_user_id:
                action = amo.LOG.ADD_USER_WITH_ROLE
                author.addon = addon
            elif author.role != author._original_role:
                action = amo.LOG.CHANGE_USER_WITH_ROLE

            author.save()
            if action:
                amo.log(action, author.user, author.get_role_display(), addon)
            if (author._original_user_id and
                author.user_id != author._original_user_id):
                amo.log(amo.LOG.REMOVE_USER_WITH_ROLE,
                        (UserProfile, author._original_user_id),
                        author.get_role_display(), addon)

        for author in user_form.deleted_objects:
            amo.log(amo.LOG.REMOVE_USER_WITH_ROLE, author.user,
                    author.get_role_display(), addon)

        _license_form(request, addon, save=True)
        _policy_form(request, addon, save=True)
        messages.success(request, _('Changes successfully saved.'))
        return redirect('devhub.addons.owner', addon.slug)

    ctx.update(addon=addon, user_form=user_form, policy_form=policy_form)
    return jingo.render(request, 'devhub/addons/owner.html', ctx)


@dev_required(owner_for_post=True)
def payments(request, addon_id, addon):
    charity = None if addon.charity_id == amo.FOUNDATION_ORG else addon.charity
    charity_form = forms.CharityForm(request.POST or None, instance=charity,
                                     prefix='charity')
    contrib_form = forms.ContribForm(request.POST or None, instance=addon,
                                     initial=forms.ContribForm.initial(addon))
    profile_form = forms.ProfileForm(request.POST or None, instance=addon,
                                     required=True)
    if request.method == 'POST':
        if contrib_form.is_valid():
            addon = contrib_form.save(commit=False)
            addon.wants_contributions = True
            valid = _save_charity(addon, contrib_form, charity_form)
            if not addon.has_full_profile():
                valid &= profile_form.is_valid()
                if valid:
                    profile_form.save()
            if valid:
                addon.save()
                messages.success(request, _('Changes successfully saved.'))
                amo.log(amo.LOG.EDIT_CONTRIBUTIONS, addon)

                return redirect('devhub.addons.payments', addon.slug)
    errors = charity_form.errors or contrib_form.errors or profile_form.errors
    if errors:
        messages.error(request, _('There were errors in your submission.'))
    return jingo.render(request, 'devhub/addons/payments.html',
        dict(addon=addon, charity_form=charity_form, errors=errors,
             contrib_form=contrib_form, profile_form=profile_form))


def _save_charity(addon, contrib_form, charity_form):
    recipient = contrib_form.cleaned_data['recipient']
    if recipient == 'dev':
        addon.charity = None
    elif recipient == 'moz':
        addon.charity_id = amo.FOUNDATION_ORG
    elif recipient == 'org':
        if charity_form.is_valid():
            addon.charity = charity_form.save()
        else:
            return False
    return True


@dev_required
@post_required
def disable_payments(request, addon_id, addon):
    addon.update(wants_contributions=False)
    return redirect('devhub.addons.payments', addon.slug)


@dev_required
@post_required
def remove_profile(request, addon_id, addon):
    delete_translation(addon, 'the_reason')
    delete_translation(addon, 'the_future')
    if addon.wants_contributions:
        addon.update(wants_contributions=False)
    return redirect('devhub.addons.profile', addon.slug)


@dev_required
def profile(request, addon_id, addon):
    profile_form = forms.ProfileForm(request.POST or None, instance=addon)

    if request.method == 'POST' and profile_form.is_valid():
        profile_form.save()
        amo.log(amo.LOG.EDIT_PROPERTIES, addon)
        messages.success(request, _('Changes successfully saved.'))
        return redirect('devhub.addons.profile', addon.slug)

    return jingo.render(request, 'devhub/addons/profile.html',
                        dict(addon=addon, profile_form=profile_form))


@login_required
def validate_addon(request):
    return jingo.render(request, 'devhub/validate_addon.html', {})


@login_required
@post_required
def upload(request):
    if request.method == 'POST':
        filedata = request.FILES['upload']

        fu = FileUpload.from_post(filedata, filedata.name, filedata.size)
        if request.user.is_authenticated():
            fu.user = request.amo_user
            fu.save()
        tasks.validator.delay(fu.pk)
        return redirect('devhub.upload_detail', fu.pk, 'json')

    return jingo.render(request, 'devhub/upload.html')


def escape_all(v):
    """Escape html in JSON value, including nested list items."""
    if isinstance(v, basestring):
        v = jinja2.escape(v)
        v = bleach.linkify(v, nofollow=True,
                           filter_url=urlresolvers.get_outgoing_url)
        return v
    elif isinstance(v, list):
        for i, lv in enumerate(v):
            v[i] = escape_all(lv)

    return v


def make_validation_result(data):
    """Safe wrapper around JSON dict containing a validation result."""
    if not settings.EXPOSE_VALIDATOR_TRACEBACKS:
        if data['error']:
            # Just expose the message, not the traceback
            data['error'] = data['error'].strip().split('\n')[-1].strip()
    if data['validation']:
        for msg in data['validation']['messages']:
            if msg['tier'] == 0:
                # We can't display a message if it's on tier 0.
                # Should get fixed soon in bug 617481
                msg['tier'] = 1
            for k, v in msg.items():
                msg[k] = escape_all(v)
    return data


@dev_required(allow_editors=True)
def file_validation(request, addon_id, addon, file_id):
    file = get_object_or_404(File, id=file_id)

    v = reverse('devhub.json_file_validation', args=[addon.slug, file.id])
    return jingo.render(request, 'devhub/validation.html',
                        dict(validate_url=v, filename=file.filename,
                             timestamp=file.created,
                             addon=addon))


@dev_required(allow_editors=True)
def validation_result(request, addon_id, addon, result_id):
    qs = ValidationResult.objects.exclude(completed=None)
    result = get_object_or_404(qs, pk=result_id)

    v = reverse('devhub.json_validation_result', args=[addon.slug, result.id])
    app_trans = dict((g, unicode(a.pretty)) for g, a in amo.APP_GUIDS.items())
    ff_versions = (AppVersion.objects.filter(application=amo.FIREFOX.id,
                                             version_int__gte=4000000000000)
                   .values_list('application', 'version')
                   .order_by('version_int'))
    tpl = 'https://developer.mozilla.org/en/Firefox_%s_for_developers'
    change_links = dict()
    for app, ver in ff_versions:
        major = ver[0]  # 4.0b3 -> 4
        change_links['%s %s' % (amo.APP_IDS[app].guid, ver)] = tpl % major
    return jingo.render(request, 'devhub/validation.html',
                        dict(validate_url=v, filename=result.file.filename,
                             timestamp=result.completed,
                             job=result.validation_job,
                             addon=result.file.version.addon,
                             result_type='compat',
                             app_trans=app_trans,
                             version_change_links=change_links))


@json_view
@csrf_view_exempt
@post_required
@dev_required(allow_editors=True)
def json_file_validation(request, addon_id, addon, file_id):
    file = get_object_or_404(File, id=file_id)
    if not file.has_been_validated:
        try:
            v_result = tasks.file_validator(file.id)
        except Exception, exc:
            log.error('file_validator(%s): %s' % (file.id, exc))
            error = "\n".join(traceback.format_exception(*sys.exc_info()))
            return make_validation_result({'validation': '',
                                           'error': error})
    else:
        v_result = file.validation
    validation = json.loads(v_result.validation)

    return make_validation_result(dict(validation=validation,
                                       error=None))


@json_view
@csrf_view_exempt
@post_required
@dev_required(allow_editors=True)
def json_validation_result(request, addon_id, addon, result_id):
    qs = ValidationResult.objects.exclude(completed=None)
    result = get_object_or_404(qs, pk=result_id)
    if result.task_error:
        return make_validation_result({'validation': '',
                                       'error': result.task_error})
    else:
        validation = json.loads(result.validation)
        return make_validation_result(dict(validation=validation, error=None))


@json_view
def json_upload_detail(upload):
    if not settings.VALIDATE_ADDONS:
        upload.task_error = ''
        upload.validation = json.dumps({'errors': 0, 'messages': [],
                                        'notices': 0, 'warnings': 0})
        upload.save()

    validation = json.loads(upload.validation) if upload.validation else ""
    url = reverse('devhub.upload_detail', args=[upload.uuid, 'json'])
    full_report_url = reverse('devhub.upload_detail', args=[upload.uuid])
    plat_exclude = []

    if validation:
        if validation['errors'] == 0:
            try:
                apps = parse_addon(upload.path).get('apps', [])
                app_ids = set([a.id for a in apps])
                supported_platforms = []
                if amo.MOBILE.id in app_ids:
                    supported_platforms.extend(amo.MOBILE_PLATFORMS.keys())
                    app_ids.remove(amo.MOBILE.id)
                if len(app_ids):
                    # Targets any other non-mobile app:
                    supported_platforms.extend(amo.DESKTOP_PLATFORMS.keys())
                s = amo.SUPPORTED_PLATFORMS.keys()
                plat_exclude = set(s) - set(supported_platforms)
                plat_exclude = [str(p) for p in plat_exclude]
            except django_forms.ValidationError, exc:
                # XPI parsing errors will be reported in the form submission
                # (next request).
                # TODO(Kumar) It would be nicer to present errors to the user
                # right here to avoid confusion about platform selection.
                log.error("XPI parsing error, ignored: %s" % exc)

    return make_validation_result(dict(upload=upload.uuid,
                                       validation=validation,
                                       error=upload.task_error, url=url,
                                       full_report_url=full_report_url,
                                       platforms_to_exclude=plat_exclude))


def upload_detail(request, uuid, format='html'):
    upload = get_object_or_404(FileUpload.uncached, uuid=uuid)

    if format == 'json' or request.is_ajax():
        return json_upload_detail(upload)

    v = reverse('devhub.upload_detail', args=[upload.uuid, 'json'])
    return jingo.render(request, 'devhub/validation.html',
                        dict(validate_url=v, filename=upload.name,
                             timestamp=upload.created,
                             addon=None))


@dev_required
def addons_section(request, addon_id, addon, section, editable=False):
    models = {'basic': addon_forms.AddonFormBasic,
              'media': addon_forms.AddonFormMedia,
              'details': addon_forms.AddonFormDetails,
              'support': addon_forms.AddonFormSupport,
              'technical': addon_forms.AddonFormTechnical,
              'admin': forms.AdminForm,
    }

    if section not in models:
        return http.HttpResponseNotFound()

    tags = previews = restricted_tags = []
    cat_form = None

    if section == 'basic':
        tags = addon.tags.not_blacklisted().values_list('tag_text', flat=True)
        cat_form = addon_forms.CategoryFormSet(request.POST or None,
                                               addon=addon)
        restricted_tags = addon.tags.filter(restricted=True)

    elif section == 'media':
        previews = forms.PreviewFormSet(request.POST or None,
                prefix='files', queryset=addon.previews.all())

    # Get the slug before the form alters it to the form data.
    valid_slug = addon.slug
    if editable:
        if request.method == 'POST':
            form = models[section](request.POST, request.FILES,
                                  instance=addon, request=request)
            if (form.is_valid() and (not previews or previews.is_valid()) and
                (section != 'basic' or (cat_form and cat_form.is_valid()))):
                addon = form.save(addon)

                if previews:
                    for preview in previews.forms:
                        preview.save(addon)

                editable = False
                if section == 'media':
                    amo.log(amo.LOG.CHANGE_ICON, addon)
                else:
                    amo.log(amo.LOG.EDIT_PROPERTIES, addon)

                if cat_form:
                    cat_form.save()

                valid_slug = addon.slug
        else:
            form = models[section](instance=addon, request=request)
    else:
        form = False

    data = {'addon': addon,
            'form': form,
            'editable': editable,
            'tags': tags,
            'restricted_tags': restricted_tags,
            'cat_form': cat_form,
            'preview_form': previews,
            'valid_slug': valid_slug,
            }

    return jingo.render(request,
                        'devhub/addons/edit/%s.html' % section, data)


@never_cache
@dev_required
@json_view
def image_status(request, addon_id, addon):
    # Default icon needs no checking.
    if not addon.icon_type or addon.icon_type.split('/')[0] == 'icon':
        icons = True
    # Persona icon is handled differently.
    elif addon.type == amo.ADDON_PERSONA:
        icons = True
    else:
        icons = os.path.exists(os.path.join(addon.get_icon_dir(),
                                            '%s-32.png' % addon.id))
    previews = all(os.path.exists(p.thumbnail_path)
                   for p in addon.previews.all())
    return {'overall': icons and previews,
            'icons': icons,
            'previews': previews}


@json_view
@dev_required
def upload_image(request, addon_id, addon, upload_type):
    errors = []
    upload_hash = ''

    if 'upload_image' in request.FILES:
        upload_preview = request.FILES['upload_image']
        upload_preview.seek(0)

        upload_hash = uuid.uuid4().hex
        loc = path.path(settings.TMP_PATH) / upload_type / upload_hash
        if not loc.dirname().exists():
            loc.dirname().makedirs()

        with open(loc, 'wb') as fd:
            for chunk in upload_preview:
                fd.write(chunk)

        check = amo.utils.ImageCheck(upload_preview)
        if (not check.is_image() or
            upload_preview.content_type not in
            ('image/png', 'image/jpeg', 'image/jpg')):
            errors.append(_('Icons must be either PNG or JPG.'))

        if check.is_animated():
            errors.append(_('Icons cannot be animated.'))

        if (upload_type == 'icon' and
            upload_preview.size > settings.MAX_ICON_UPLOAD_SIZE):
            errors.append(_('Please use images smaller than %dMB.') %
                        (settings.MAX_ICON_UPLOAD_SIZE / 1024 / 1024 - 1))
    else:
        errors.append(_('There was an error uploading your preview.'))

    if errors:
        upload_hash = ''

    return {'upload_hash': upload_hash, 'errors': errors}


@dev_required
def version_edit(request, addon_id, addon, version_id):
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    version_form = forms.VersionForm(request.POST or None, instance=version)

    new_file_form = forms.NewFileForm(request.POST or None,
                                      addon=addon, version=version)

    file_form = forms.FileFormSet(request.POST or None, prefix='files',
                                  queryset=version.files.all())
    file_history = _get_file_history(version)

    data = {'version_form': version_form, 'file_form': file_form}

    if addon.accepts_compatible_apps():
        # We should be in no-caching land but this one stays cached for some
        # reason.
        qs = version.apps.all().no_cache()
        compat_form = forms.CompatFormSet(request.POST or None, queryset=qs)
        data['compat_form'] = compat_form

    if (request.method == 'POST' and
        all([form.is_valid() for form in data.values()])):
        data['version_form'].save()
        data['file_form'].save()

        for deleted in data['file_form'].deleted_forms:
            file = deleted.cleaned_data['id']
            amo.log(amo.LOG.DELETE_FILE_FROM_VERSION,
                    file.filename, file.version, addon)

        if 'compat_form' in data:
            for compat in data['compat_form'].save(commit=False):
                compat.version = version
                compat.save()
        messages.success(request, _('Changes successfully saved.'))
        return redirect('devhub.versions.edit', addon.slug, version_id)

    data.update(addon=addon, version=version, new_file_form=new_file_form,
                file_history=file_history)
    return jingo.render(request, 'devhub/versions/edit.html', data)


def _get_file_history(version):
    file_ids = [f.id for f in version.all_files]
    addon = version.addon
    file_history = (ActivityLog.objects.for_addons(addon)
                               .filter(action__in=amo.LOG_REVIEW_QUEUE))
    files = dict([(fid, []) for fid in file_ids])
    for log in file_history:
        details = log.details
        current_file_ids = details["files"] if 'files' in details else []
        for fid in current_file_ids:
            if fid in file_ids:
                files[fid].append(log)

    return files


@dev_required
@post_required
def version_delete(request, addon_id, addon):
    version_id = request.POST.get('version_id')
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    messages.success(request, _('Version %s deleted.') % version.version)
    version.delete()
    return redirect('devhub.versions', addon.slug)


@json_view
@dev_required
@post_required
def version_add(request, addon_id, addon):
    form = forms.NewVersionForm(request.POST, addon=addon)
    if form.is_valid():
        pl = (list(form.cleaned_data['desktop_platforms']) +
              list(form.cleaned_data['mobile_platforms']))
        v = Version.from_upload(form.cleaned_data['upload'], addon, pl)
        url = reverse('devhub.versions.edit', args=[addon.slug, str(v.id)])
        return dict(url=url)
    else:
        return json_view.error(form.errors)


@json_view
@dev_required
@post_required
def version_add_file(request, addon_id, addon, version_id):
    version = get_object_or_404(Version, pk=version_id, addon=addon)
    form = forms.NewFileForm(request.POST, addon=addon, version=version)
    if not form.is_valid():
        return json_view.error(form.errors)
    upload = form.cleaned_data['upload']
    new_file = File.from_upload(upload, version, form.cleaned_data['platform'])
    upload.path.unlink()
    file_form = forms.FileFormSet(prefix='files', queryset=version.files.all())
    form = [f for f in file_form.forms if f.instance == new_file]
    return jingo.render(request, 'devhub/includes/version_file.html',
                        {'form': form[0], 'addon': addon})


@dev_required
def version_list(request, addon_id, addon):
    qs = addon.versions.order_by('-created').transform(Version.transformer)
    versions = amo.utils.paginate(request, qs)
    new_file_form = forms.NewVersionForm(None, addon=addon)

    data = {'addon': addon,
            'versions': versions,
            'new_file_form': new_file_form,
            'position': get_position(addon),
            'timestamp': int(time.time())}
    return jingo.render(request, 'devhub/versions/list.html', data)


@dev_required
def version_bounce(request, addon_id, addon, version):
    # Use filter since there could be dupes.
    vs = (Version.objects.filter(version=version, addon=addon)
          .order_by('-created'))
    if vs:
        return redirect('devhub.versions.edit', addon.slug, vs[0].id)
    else:
        raise http.Http404()


@json_view
@dev_required
def version_stats(request, addon_id, addon):
    qs = Version.objects.filter(addon=addon)
    reviews = (qs.annotate(reviews=Count('reviews'))
               .values('id', 'version', 'reviews'))
    d = dict((v['id'], v) for v in reviews)
    files = qs.annotate(files=Count('files')).values_list('id', 'files')
    for id, files in files:
        d[id]['files'] = files
    return d


Step = collections.namedtuple('Step', 'current max')


def submit_step(step):
    """Wraps the function with a decorator that bounces to the right step."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(request, *args, **kw):
            max_step = 7
            # We only bounce on pages with an addon id.
            if 'addon' in kw:
                addon = kw['addon']
                on_step = SubmitStep.objects.filter(addon=addon)
                if on_step:
                    max_step = on_step[0].step
                    if max_step < step:
                        # The step was too high, so bounce to the saved step.
                        return redirect('devhub.submit.%s' % max_step,
                                        addon.slug)
                elif step != max_step:
                    # We couldn't find a step, so we must be done.
                    return redirect('devhub.submit.7', addon.slug)
            kw['step'] = Step(step, max_step)
            return f(request, *args, **kw)
        # Tell @dev_required that this is a function in the submit flow so it
        # doesn't try to redirect into the submit flow.
        wrapper.submitting = True
        return wrapper
    return decorator


@login_required
@submit_step(1)
def submit(request, step):
    if request.method == 'POST':
        response = redirect('devhub.submit.2')
        response.set_cookie(DEV_AGREEMENT_COOKIE)
        return response

    base = os.path.join(os.path.dirname(amo.__file__), '..', '..', 'locale')
    # Note that the agreement is not localized (for legal reasons)
    # but the official version is stored in en_US.
    agrmt = os.path.join(base,
                'en_US', 'pages', 'docs', 'policies', 'agreement.thtml')
    f = codecs.open(agrmt, encoding='utf8')
    agreement_text = f.read()
    f.close()

    return jingo.render(request, 'devhub/addons/submit/start.html',
                        {'agreement_text': agreement_text, 'step': step})


@login_required
@submit_step(2)
def submit_addon(request, step):
    if DEV_AGREEMENT_COOKIE not in request.COOKIES:
        return redirect('devhub.submit.1')
    form = forms.NewAddonForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            data = form.cleaned_data
            p = (list(data['desktop_platforms']) +
                 list(data['mobile_platforms']))
            addon = Addon.from_upload(data['upload'], p)
            AddonUser(addon=addon, user=request.amo_user).save()
            SubmitStep.objects.create(addon=addon, step=3)
            return redirect('devhub.submit.3', addon.slug)
    return jingo.render(request, 'devhub/addons/submit/upload.html',
                        {'step': step, 'new_addon_form': form})


@dev_required
@submit_step(3)
def submit_describe(request, addon_id, addon, step):
    form = forms.Step3Form(request.POST or None, instance=addon,
                           request=request)
    cat_form = addon_forms.CategoryFormSet(request.POST or None, addon=addon)
    if request.method == 'POST' and form.is_valid() and cat_form.is_valid():
        addon = form.save(addon)
        cat_form.save()
        SubmitStep.objects.filter(addon=addon).update(step=4)
        return redirect('devhub.submit.4', addon.slug)
    return jingo.render(request, 'devhub/addons/submit/describe.html',
                        {'form': form, 'cat_form': cat_form, 'addon': addon,
                         'step': step})


@dev_required
@submit_step(4)
def submit_media(request, addon_id, addon, step):
    form_icon = addon_forms.AddonFormMedia(request.POST or None,
            request.FILES, instance=addon, request=request)
    form_previews = forms.PreviewFormSet(request.POST or None,
            prefix='files', queryset=addon.previews.all())

    if (request.method == 'POST' and
        form_icon.is_valid() and form_previews.is_valid()):
        addon = form_icon.save(addon)

        for preview in form_previews.forms:
            preview.save(addon)

        SubmitStep.objects.filter(addon=addon).update(step=5)
        return redirect('devhub.submit.5', addon.slug)

    return jingo.render(request, 'devhub/addons/submit/media.html',
                        {'form': form_icon, 'addon': addon, 'step': step,
                         'preview_form': form_previews})


@dev_required
@submit_step(5)
def submit_license(request, addon_id, addon, step):
    fs, ctx = [], {}
    # Versions.
    ctx.update(_license_form(request, addon))
    fs.append(ctx['license_form'])
    # Policy.
    policy_form = _policy_form(request, addon)
    fs.append(policy_form)
    if request.method == 'POST' and all([form.is_valid() for form in fs]):
        _license_form(request, addon, save=True, log=False)
        _policy_form(request, addon, save=True)
        SubmitStep.objects.filter(addon=addon).update(step=6)
        return redirect('devhub.submit.6', addon.slug)
    ctx.update(addon=addon, policy_form=policy_form, step=step)
    return jingo.render(request, 'devhub/addons/submit/license.html', ctx)


@dev_required
@submit_step(6)
def submit_select_review(request, addon_id, addon, step):
    review_type_form = forms.ReviewTypeForm(request.POST or None)
    if request.method == 'POST' and review_type_form.is_valid():
        addon.status = review_type_form.cleaned_data['review_type']
        addon.save()
        SubmitStep.objects.filter(addon=addon).delete()
        signals.submission_done.send(sender=addon)
        return redirect('devhub.submit.7', addon.slug)
    return jingo.render(request, 'devhub/addons/submit/select-review.html',
                        {'addon': addon, 'review_type_form': review_type_form,
                         'step': step})


@dev_required
@submit_step(7)
def submit_done(request, addon_id, addon, step):
    # Bounce to the versions page if they don't have any versions.
    if not addon.versions.exists():
        return redirect('devhub.versions', addon.slug)
    sp = addon.current_version.supported_platforms
    is_platform_specific = sp != [amo.PLATFORM_ALL]

    return jingo.render(request, 'devhub/addons/submit/done.html',
                        {'addon': addon, 'step': step,
                         'is_platform_specific': is_platform_specific})


@dev_required
def submit_resume(request, addon_id, addon):
    step = SubmitStep.objects.filter(addon=addon)
    return _resume(addon, step)


def _resume(addon, step):
    if step:
        return redirect('devhub.submit.%s' % step[0].step, addon.slug)

    return redirect('devhub.versions', addon.slug)


@login_required
@dev_required
def submit_bump(request, addon_id, addon):
    if not acl.action_allowed(request, 'Admin', 'EditSubmitStep'):
        return http.HttpResponseForbidden()
    step = SubmitStep.objects.filter(addon=addon)
    step = step[0] if step else None
    if request.method == 'POST' and request.POST.get('step'):
        new_step = request.POST['step']
        if step:
            step.step = new_step
        else:
            step = SubmitStep(addon=addon, step=new_step)
        step.save()
        return redirect('devhub.submit.bump', addon.slug)
    return jingo.render(request, 'devhub/addons/submit/bump.html',
                        dict(addon=addon, step=step))


@dev_required
@post_required
def remove_locale(request, addon_id, addon):
    POST = request.POST
    if 'locale' in POST and POST['locale'] != addon.default_locale:
        addon.remove_locale(POST['locale'])
        return http.HttpResponse()
    return http.HttpResponseBadRequest()


# You can only request one of the new review tracks.
REQUEST_REVIEW = (amo.STATUS_PUBLIC, amo.STATUS_LITE)


@dev_required
@post_required
def request_review(request, addon_id, addon, status):
    status_req = int(status)
    if status_req not in addon.can_request_review():
        return http.HttpResponseBadRequest()
    elif status_req == amo.STATUS_PUBLIC:
        if addon.status == amo.STATUS_LITE:
            new_status = amo.STATUS_LITE_AND_NOMINATED
        else:
            new_status = amo.STATUS_NOMINATED
    elif status_req == amo.STATUS_LITE:
        if addon.status in (amo.STATUS_PUBLIC, amo.STATUS_LITE_AND_NOMINATED):
            new_status = amo.STATUS_LITE
        else:
            new_status = amo.STATUS_UNREVIEWED

    addon.update(status=new_status)
    msg = {amo.STATUS_LITE: _('Preliminary Review Requested.'),
           amo.STATUS_PUBLIC: _('Full Review Requested.')}
    messages.success(request, msg[status_req])
    amo.log(amo.LOG.CHANGE_STATUS, addon.get_status_display(), addon)
    return redirect('devhub.versions', addon.slug)


# TODO(kumar): Remove when the editor tools are in zamboni.
def validator_redirect(request, version_id):
    v = get_object_or_404(Version, id=version_id)
    return redirect('devhub.versions', v.addon_id, permanent=True)


@post_required
@addon_view
def admin(request, addon):
    if not acl.action_allowed(request, 'Admin', 'ConfigureAnyAddon'):
        return http.HttpResponseForbidden()
    form = forms.AdminForm(request, request.POST or None, instance=addon)
    if form.is_valid():
        form.save()
    return jingo.render(request, 'devhub/addons/edit/admin.html',
                        {'addon': addon, 'admin_form': form})


def docs(request, doc_name=None, doc_page=None):
    filename = ''

    all_docs = {'getting-started': [], 'reference': [],
                'policies': ['submission', 'reviews', 'maintenance',
                             'recommended', 'agreement', 'contact'],
                'case-studies': ['cooliris', 'stumbleupon',
                                 'download-statusbar'],
                'how-to': ['getting-started', 'extension-development',
                           'thunderbird-mobile', 'theme-development',
                           'other-addons']}

    if doc_name and doc_name in all_docs:
        filename = '%s.html' % doc_name
        if doc_page and doc_page in all_docs[doc_name]:
            filename = '%s-%s.html' % (doc_name, doc_page)

    if not filename:
        return redirect('devhub.index')

    return jingo.render(request, 'devhub/docs/%s' % filename)


def builder(request):
    return jingo.render(request, 'devhub/builder.html')


@anonymous_csrf
def newsletter(request):
    regions = get_regions(getattr(request, 'LANG', settings.LANGUAGE_CODE))
    form = forms.NewsletterForm(request.POST or None,
                                regions=sorted(regions.iteritems(),
                                               key=operator.itemgetter(1)))

    if request.method == 'POST':
        if form.is_valid():
            data = form.cleaned_data
            responsys.subscribe.delay(
                'ABOUT_ADDONS', data['email'], data['format'],
                responsys.make_source_url(request), request.LANG,
                data['region'])
            messages.success(request, _('Thanks for subscribing!'))
            return redirect('devhub.community.newsletter')

    return jingo.render(request, 'devhub/newsletter.html',
                        {'newsletter_form': form})
