#!/usr/bin/env python
import webapp2
from google.appengine.api import app_identity
from google.appengine.api import mail
from conference import ConferenceApi
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.ext import ndb
from models import Session

# Handlers for taskqueues

class SendConfirmationEmailHandler(webapp2.RequestHandler):
    def post(self):
        """Send email confirming Conference creation."""
        mail.send_mail(
            'noreply@%s.appspotmail.com' % (
                app_identity.get_application_id()),     # from
            self.request.get('email'),                  # to
            'You created a new Conference!',            # subj
            'Hi, you have created a following '         # body
            'conference:\r\n\r\n%s' % self.request.get(
                'conferenceInfo')
        )

class SetAnnouncementHandler(webapp2.RequestHandler):
    def get(self):
        """Set Announcement in Memcache."""       
        ConferenceApi._cacheAnnouncement()

# If the speaker hosts at least one other session in this conference,
# set them as the new featured speaker. This is called when a new 
# session is added to the conference.
class SetFeaturedSpeaker(webapp2.RequestHandler):  
    def get(self):
        conf = ndb.Key(urlsafe=self.request.get('websafeConferenceKey'))
        sessions = Session.query(ancestor=conf)
        speaker_sessions = sessions.filter(Session.speaker == self.request.get('speaker'))
        speaker_session_names = [self.request.get('speaker')]
        for sess in speaker_sessions:
            speaker_session_names.append(sess.name)
        if len(speaker_session_names) > 2:
            memcache.set(key='featured_speaker_sessions', value=speaker_session_names)

# Set URL's for each handler
app = webapp2.WSGIApplication([
    ('/crons/set_announcement', SetAnnouncementHandler),
    ('/tasks/send_confirmation_email', SendConfirmationEmailHandler),
    ('/tasks/set_featured_speaker', SetFeaturedSpeaker),
], debug=True)