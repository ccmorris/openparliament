import datetime
import logging
import re
import time
import urllib2
from urlparse import urljoin

from django.db import transaction

import lxml.html
import requests

from parliament.committees.models import (Committee, CommitteeMeeting,
    CommitteeActivity, CommitteeActivityInSession,
    CommitteeInSession)
from parliament.core.models import Session
from parliament.hansards.models import Document

logger = logging.getLogger(__name__)

COMMITTEE_LIST_URL = 'http://www.parl.gc.ca/Committees/en/List?parl=%d&session=%d'
@transaction.atomic
def import_committee_list(session=None):
    if session is None:
        session = Session.objects.current()

    def make_committee(name_en, acronym, parent=None):
        try:
            return Committee.objects.get_by_acronym(acronym, session)
        except Committee.DoesNotExist:
            committee, created = Committee.objects.get_or_create(name_en=name_en.strip(), parent=parent)
            if created:
                logger.warning(u"Creating committee: %s, %s" % (committee.name_en, committee.slug))
            CommitteeInSession.objects.get_or_create(
                committee=committee, session=session, acronym=acronym)
            return committee
    
    resp = requests.get(COMMITTEE_LIST_URL % (session.parliamentnum, session.sessnum))
    resp.raise_for_status()
    root = lxml.html.fromstring(resp.text)

    found = False
    for cmte_div in root.cssselect('.committees-list .accordion-item'):
        acronym = cmte_div.cssselect('.accordion-bar-title .committee-acronym-cell')
        assert len(acronym) == 1
        acronym = acronym[0].text_content()

        name = cmte_div.cssselect('.accordion-bar-title .committee-name')
        assert len(name) == 1
        name = name[0].text_content()
        com = make_committee(name, acronym)
        found = True

        for sub in cmte_div.cssselect('.subcommittee-item .subcommittee-name'):
            match = re.search(r'^(.+) \(([A-Z0-9]{3,5})\)$', sub.text_content())
            (name, acronym) = match.groups()
            make_committee(name, acronym, parent=com)

    if not found:
        logger.error("No committees in list")
            
    return True

def _docid_from_url(u):
    return int(re.search(r'(Doc|publication)Id=(\d+)&', u).group(2))
    
def _12hr(hour, ampm):
    hour = int(hour)
    hour += 12 * bool('p' in ampm.lower())
    if hour % 12 == 0:
        # noon, midnight
        hour -= 12
    return hour
    
def _parse_date(d):
    """datetime objects from e.g. March 11, 2011"""
    return datetime.date(
        *time.strptime(d, '%B %d, %Y')[:3]
    )


def import_committee_documents(session):
    for comm in Committee.objects.filter(sessions=session).order_by('-parent'):
        # subcommittees last
        import_committee_meetings(comm, session)
        #import_committee_reports(comm, session)
        #time.sleep(1)

COMMITTEE_MEETINGS_URL = 'http://www.parl.gc.ca/Committees/en/%(acronym)s/Meetings?parl=%(parliamentnum)d&session=%(sessnum)d'
@transaction.atomic
def import_committee_meetings(committee, session):

    acronym = committee.get_acronym(session)
    url = COMMITTEE_MEETINGS_URL % {'acronym': acronym,
        'parliamentnum': session.parliamentnum,
        'sessnum': session.sessnum}
    resp = urllib2.urlopen(url)
    tree = lxml.html.parse(resp)
    root = tree.getroot()
    for mtg_row in root.cssselect('#meeting-accordion .accordion-item'):
        source_id = mtg_row.get('id')
        assert source_id.startswith('meeting-item-')
        source_id = int(source_id.replace('meeting-item-', '').strip())

        number = int(re.sub(r'\D', '', mtg_row.cssselect('.meeting-title .meeting-number')[0].text))
        assert number > 0

        cancelled = bool(mtg_row.cssselect('.meeting-title .icon-cancel'))
        if cancelled:
            try:
                mtg = CommitteeMeeting.objects.get(committee=committee, session=session,
                    number=number, source_id=source_id)
                assert not mtg.evidence_id
                mtg.delete()
                logger.warning("Deleting %s cancelled meeting #%d source_id %s", committee, number, source_id)
            except CommitteeMeeting.DoesNotExist:
                pass
            continue

        try:
            meeting = CommitteeMeeting.objects.select_related('evidence').get(
                committee=committee, session=session, number=number)
        except CommitteeMeeting.DoesNotExist:
            meeting = CommitteeMeeting(committee=committee,
                session=session, number=number)
        
        if meeting.source_id:
            if meeting.source_id != source_id:
                if meeting.evidence_id:
                    logger.error("Source ID mismatch for %s meeting %s (orig %s new %s)" % (
                        committee, number, meeting.source_id, source_id))
                    continue
                else:
                    # As long as there was no evidence loaded, just replace the old meeting
                    # with the new one
                    meeting.delete()
                    meeting = CommitteeMeeting(committee=committee, source_id=source_id,
                        session=session, number=number)
        else:
            meeting.source_id = source_id
            if meeting.id:
                meeting.save()

        date_string = mtg_row.cssselect('.meeting-title .date-label')[0].text_content().strip()
        if date_string in ('Earlier Today', 'Later Today', 'In Progress', 'Tomorrow', 'Yesterday'):
            match = re.search(r'-(20\d\d)-(\d\d)-(\d\d)', mtg_row.get('class'))
            assert match
            meeting.date = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        else:
            meeting.date = _parse_date(date_string.partition(', ')[2]) # partition is to split off day of week
        
        timestring = mtg_row.cssselect('.the-time')[0].text_content()
        match = re.search(r'(\d\d?):(\d\d) ([ap]\.?m\.?)(?: - (\d\d?):(\d\d) ([ap]\.?m\.?))?\s\(',
            timestring, re.UNICODE)
        meeting.start_time = datetime.time(_12hr(match.group(1), match.group(3)), int(match.group(2)))
        if match.group(4):
            meeting.end_time = datetime.time(_12hr(match.group(4), match.group(6)), int(match.group(5)))
        
        notice_link = mtg_row.cssselect('a.btn-meeting-notice')
        if notice_link:
            meeting.notice = _docid_from_url(notice_link[0].get('href'))
        minutes_link = mtg_row.cssselect('a.btn-meeting-minutes')
        if minutes_link:
            meeting.minutes = _docid_from_url(minutes_link[0].get('href'))
        
        evidence_link = mtg_row.cssselect('a.btn-meeting-evidence')
        if evidence_link:
            evidence_id = _docid_from_url(evidence_link[0].get('href'))
            if meeting.evidence_id:
                if meeting.evidence.source_id != evidence_id:
                    raise Exception("Evidence docid mismatch for %s %s: %s %s" %
                        (committee.acronym, number, evidence_id, meeting.evidence.source_id))
                else:
                    # Evidence hasn't changed; we don't need to worry about updating
                    continue
            else:
                if Document.objects.filter(source_id=evidence_id).exists():
                    raise Exception("Found evidence source_id %s, but it already exists" % evidence_id)
                meeting.evidence = Document.objects.create(
                    source_id=evidence_id,
                    date=meeting.date,
                    session=session,
                    document_type=Document.EVIDENCE)
        
        meeting.webcast = bool(mtg_row.cssselect('.btn-meeting-parlvu'))
        meeting.in_camera = bool(mtg_row.cssselect('.meeting-title i[title*="In Camera"]'))
        if not meeting.televised:
            meeting.televised = bool(mtg_row.cssselect('.meeting-title .icon-television'))
        if not meeting.travel:
            meeting.travel = bool(mtg_row.cssselect('.meeting-title .icon-plane'))
        
        meeting.save()
        
        for study_link in mtg_row.cssselect('.meeting-card-study a'):
            name = study_link.text.strip()
            try:
                study = get_activity_by_url(study_link.get('href'),
                    committee=committee, session=session)
                meeting.activities.add(study)
            except:
                logger.exception("Error fetching committee activity for %r %s %s",
                    committee, name, study_link.get('href'))
    
    return True

def get_activity_by_url(activity_url, committee, session):
    activity_id = int(re.search(r'(studyActivityId|Stac)=(\d+)', activity_url).group(2))
    activity_url = urljoin(COMMITTEE_MEETINGS_URL, activity_url)

    try:
        return CommitteeActivityInSession.objects.get(source_id=activity_id).activity
    except CommitteeActivityInSession.DoesNotExist:
        pass

    activity = CommitteeActivity(committee=committee)
    activity.study = True # not parsing this at the moment
    root = lxml.html.parse(urllib2.urlopen(activity_url)).getroot()

    activity.name_en = root.cssselect('.core-content h2')[0].text.strip()[:500]

    # See if this already exists for another session
    try:
        activity = CommitteeActivity.objects.get(
            committee=activity.committee,
            # study=activity.study,
            name_en=activity.name_en
        )
    except CommitteeActivity.DoesNotExist:
        url = activity_url.replace('/en/', '/fr/')
        root = lxml.html.parse(urllib2.urlopen(url)).getroot()
        activity.name_fr = root.cssselect('.core-content h2')[0].text.strip()[:500]
        activity.save()

    if CommitteeActivityInSession.objects.exclude(source_id=activity_id).filter(
            session=session, activity=activity).exists():
        logger.info("Apparent duplicate activity ID for %s %s %s: %s" %
                     (activity, activity.committee, session, activity_id))
        return activity
    
    CommitteeActivityInSession.objects.create(
        session=session,
        activity=activity,
        source_id=activity_id
    )
    return activity

# The report scraper is for a previous version of parl.gc.ca, and has not been updated.
#
# COMMITTEE_REPORT_URL = 'http://www2.parl.gc.ca/CommitteeBusiness/ReportsResponses.aspx?Cmte=%(acronym)s&Language=E&Mode=1&Parl=%(parliamentnum)d&Ses=%(sessnum)d'
# @transaction.atomic
# def import_committee_reports(committee, session):
#     # FIXME rework to parse out the single all-reports page?
#     acronym = committee.get_acronym(session)
#     url = COMMITTEE_REPORT_URL % {'acronym': acronym,
#         'parliamentnum': session.parliamentnum,
#         'sessnum': session.sessnum}
#     tree = lxml.html.parse(urllib2.urlopen(url))
    
#     def _import_report(report_link, parent=None):
#         report_docid = _docid_from_url(report_link.get('href'))
#         try:
#             report = CommitteeReport.objects.get(committee=committee,
#                 session=session, source_id=report_docid, parent=parent)
#             if report.presented_date:
#                 # We can consider this report fully parsed
#                 return report
#         except CommitteeReport.DoesNotExist:
#             if CommitteeReport.objects.filter(source_id=report_docid).exists():
#                 if committee.parent and \
#                   CommitteeReport.objects.filter(source_id=report_docid, committee=committee.parent).exists():
#                     # Reference to parent committee report
#                     return None
#                 else:
#                     raise Exception("Duplicate report ID %s on %s" % (report_docid, url))
#             report = CommitteeReport(committee=committee,
#                 session=session, source_id=report_docid, parent=parent)
#             report_name = report_link.text.strip()
#             match = re.search(r'^Report (\d+) - (.+)', report_name)
#             if match:
#                 report.number = int(match.group(1))
#                 report.name_en = match.group(2).strip()
#             else:
#                 report.name_en = report_name
#             report.name_en = report.name_en[:500]
#             report.government_response = bool(report_link.xpath("../span[contains(., 'Government Response')]"))
        
#         match = re.search(r'Adopted by the Committee on ([a-zA-Z0-9, ]+)', report_link.tail or '')
#         if match:
#             report.adopted_date = _parse_date(match.group(1))
#         match = re.search(r'Presented to the House on ([a-zA-Z0-9, ]+)', report_link.tail or '')
#         if match:
#             report.presented_date = _parse_date(match.group(1))
#         report.save()
#         return report
            
#     for item in tree.getroot().cssselect('.TocReportItemText'):
#         report_link = item.xpath('./a')[0]
#         report = _import_report(report_link)
#         for response_link in item.cssselect('.TocResponseItemText a'):
#             _import_report(response_link, parent=report)
            
#     return True
