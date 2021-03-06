from  pyquery import PyQuery as pq
import test_utils
from nose.tools import eq_

from translations import models, widgets


class TestWidget(test_utils.TestCase):

    def test_avoid_purified_translation(self):
        # Even if we pass in a LinkifiedTranslation the widget switches to a
        # normal Translation before rendering.
        w = widgets.TransTextarea.widget()
        link = models.LinkifiedTranslation(localized_string='<b>yum yum</b>',
                                           locale='fr', id=10)
        link.clean()
        widget = w.render('name', link)
        eq_(pq(widget).html(), '<b>yum yum</b>')
