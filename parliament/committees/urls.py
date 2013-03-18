from django.conf.urls import *

from parliament.committees.views import *

urlpatterns = patterns('parliament.committees.views',
    url(r'^$', 'committee_list', name='committee_list'),
    url(r'^activities/(?P<activity_id>\d+)/$', 'committee_activity', name='committee_activity'),
    url(r'^(?P<slug>[^/]+)/(?P<year>2\d\d\d)/$', 'committee_year_archive', name='committee_year_archive'),
    url(r'^(?P<committee_slug>[^/]+)/meetings/$', CommitteeMeetingListView.as_view(), name='committee_meetings'),
    url(r'^(?P<committee_slug>[^/]+)/(?P<session_id>\d+-\d)/(?P<number>\d+)/$', 'committee_meeting', name='committee_meeting'),
    url(r'^(?P<committee_slug>[^/]+)/(?P<session_id>\d+-\d)/(?P<number>\d+)/speeches/$', CommitteeMeetingSpeechesView.as_view(), name='committee_meeting_speeches'),
    url(r'^(?P<committee_slug>[^/]+)/(?P<session_id>\d+-\d)/(?P<number>\d+)/(?P<slug>[a-zA-Z0-9-]+)/$', 'committee_meeting_statement', name='committee_meeting'),
    url(r'^(?P<committee_slug>[^/]+)/(?P<session_id>\d+-\d)/(?P<number>\d+)/(?P<slug>[a-zA-Z0-9-]+)/only/$', 'evidence_permalink', name='evidence_permalink'),
    (r'^(?P<committee_id>\d+)/', 'committee_id_redirect'),
    (r'^(?P<slug>[^/]+)/$', 'committee'),
)