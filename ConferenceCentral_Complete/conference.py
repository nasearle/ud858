#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
import json
import os
import time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import urlfetch
from google.appengine.ext import ndb

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize

from settings import WEB_CLIENT_ID
from utils import getUserId

from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import BooleanMessage
from models import ConflictException

from google.appengine.api import memcache
from models import StringMessage
from models import FeaturedSpeaker

from google.appengine.api import taskqueue

from models import Session
from models import SessionForm
from models import SessionForms


DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESS_TYPE_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

SESS_TIME_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    startTime=messages.StringField(1),
)

SESS_DUR_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    duration=messages.StringField(1),
)

SESS_SPEAKER_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    speaker=messages.StringField(1),
)

SESS_POST_TO_WISHLIST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
         
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()  

        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(), 
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()
        return profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _copySessionToForm(self, sess):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string
                if field.name == 'date' or field.name == 'startTime':
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == 'websafeConferenceKey':
                setattr(sf, field.name, sess.key.urlsafe())
        sf.check_initialized()
        return sf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )

        return request


    def _createSessionObject(self, request):
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        
        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can add sessions to the conference.')

        # convert dates from strings to Date objects
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()

        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()

        s_id = Session.allocate_ids(size=1, parent=conf.key)[0]
        s_key = ndb.Key(Session, s_id, parent=conf.key)
        data['key'] = s_key
        data['websafeSessionKey'] = s_key.urlsafe()
        
        Session(**data).put()

        if data['speaker']:
            taskqueue.add(url='/tasks/set_featured_speaker', 
                params={'websafeConferenceKey': request.websafeConferenceKey, 
                'speaker': data['speaker']}, method='GET')                    
        
        return self._copySessionToForm(request)


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    def _updateSessionWishlist(self, request, add):
        """Add or remove session from user's wishlist"""
        retval = None
        prof = self._getProfileFromUser()
        wssk = request.websafeSessionKey
        sess = ndb.Key(urlsafe=wssk).get()
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)
        if add:
            if wssk in prof.sessionWishList:
                raise ConflictException(
                    "This Session is already in your Wishlist.")
            prof.sessionWishList.append(wssk)
            retval = True
        else:
            if wssk in prof.sessionWishList:
                prof.sessionWishList.remove(wssk)
                retval = True
            else:
                retval = False
        prof.put()
        return BooleanMessage(data=retval)


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = _getUserId()

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            print "We are going to set the announcement"
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            print "The announcement has been created. Here: ", announcement
            memcache.set("MEMCACHE_ANNOUNCEMENTS_KEY", announcement)
            print "The announcement has been set."
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete("MEMCACHE_ANNOUNCEMENTS_KEY")

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""        
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get("MEMCACHE_ANNOUNCEMENTS_KEY")
        if not announcement:            
            announcement = ""
        return StringMessage(data=announcement)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        ndb_keys = []
        prof = self._getProfileFromUser()
        websafe_keys = prof.conferenceKeysToAttend
        for key in websafe_keys:
            ndb_key = ndb.Key(urlsafe=key)
            ndb_keys.append(ndb_key)
        conferences = ndb.get_multi(ndb_keys)
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "")\
            for conf in conferences]
        )


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='sessions/wishlist', http_method='GET',
            name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get sessions in user's wishlist."""
        ndb_keys = []
        prof = self._getProfileFromUser()
        websafe_keys = prof.sessionWishList
        for key in websafe_keys:
            ndb_key = ndb.Key(urlsafe=key)
            ndb_keys.append(ndb_key)
        sessions = ndb.get_multi(ndb_keys)
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(SESS_POST_TO_WISHLIST, BooleanMessage,
            path='session/add/{websafeSessionKey}',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add session to user's wishlist"""
        return self._updateSessionWishlist(request, True)


    @endpoints.method(SESS_POST_TO_WISHLIST, BooleanMessage,
            path='session/delete/{websafeSessionKey}',
            http_method='POST', name='deleteSessionInWishlist')
    def deleteSessionInWishlist(self, request):
        """Delete session from user's wishlist"""
        return self._updateSessionWishlist(request, False)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(SESS_GET_REQUEST, SessionForms,
        path='session/{websafeConferenceKey}',
        http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return requested conference sessions (by websafeConferenceKey)."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        # make conference key
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        # create ancestor query for this conference
        sessions = Session.query(ancestor=conf.key)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with conference key: %s' % request.websafeConferenceKey)
        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
            )


    @endpoints.method(SESS_TYPE_GET_REQUEST, SessionForms,
        path='sessions/{websafeConferenceKey}/{typeOfSession}',
        http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return requested conference sessions by type (by websafeConferenceKey)."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        # make conference key
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        # create ancestor query for this conference
        sessions = Session.query(ancestor=conf.key)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with conference key: %s' % request.websafeConferenceKey)
        sessions = sessions.filter(Session.typeOfSession == request.typeOfSession)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with conference key: %s and type of session: %s'
                 % (request.websafeConferenceKey, request.typeOfSession))
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    
    @endpoints.method(SESS_DUR_GET_REQUEST, SessionForms,
        path='sessions/duration/{duration}',
        http_method='GET', name='getConferenceSessionsByDuration')
    def getConferenceSessionsByDuration(self, request):
        """Return requested conference sessions by duration."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        sessions = Session.query()
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found')
        sessions = sessions.filter(Session.duration == request.duration)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with duration: %s' % request.duration)
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )
    

    @endpoints.method(SESS_TIME_GET_REQUEST, SessionForms,
        path='sessions/time/{startTime}',
        http_method='GET', name='getConferenceSessionsByTime')
    def getConferenceSessionsByTime(self, request):
        """Return requested conference sessions by time."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        sessions = Session.query()
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found')
        new_time = datetime.strptime(request.startTime, "%H:%M").time()
        sessions = sessions.filter(Session.startTime == new_time)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with time: %s' % request.startTime)
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )


    @endpoints.method(SESS_SPEAKER_GET_REQUEST, SessionForms,
        path='sessions/speaker/{speaker}',
        http_method='GET', name='getConferenceSessionsBySpeaker')
    def getConferenceSessionsBySpeaker(self, request):
        """Return requested conference sessions by speaker."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        sessions = Session.query()
        if not sessions.get():
            raise endpoints.NotFoundException('No sessions found')
        sessions = sessions.filter(Session.speaker == request.speaker)
        if not sessions.get():
            raise endpoints.NotFoundException(
                'No sessions found with speaker: %s' % request.speaker)
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions]
        )

    
    @endpoints.method(CONF_GET_REQUEST, FeaturedSpeaker,
        path='sessions/featuredspeaker/{websafeConferenceKey}',
        http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return featured speaker and sessions for a conference from memcache."""
        featured_speaker_sessions = memcache.get('featured_speaker_sessions')
        if not featured_speaker_sessions:
            featured_speaker_sessions = ''
        return FeaturedSpeaker(data=featured_speaker_sessions)
    

    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='getConferencesCreated',
        http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
            
        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='filterPlayground', http_method='POST',
        name='filterPlayground')
    def filterPlayground(self, request):
        c = Conference.query()
        c = c.filter(Conference.city == 'London')
        c = c.filter(Conference.topics == 'Medical Innovations')
        c = c.order(Conference.name)
        c = c.filter(Conference.maxAttendees > 10)
        
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in c]
        )


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences', http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

         # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in conferences]
        )


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(SESS_POST_REQUEST, SessionForm,
                  path='session/{websafeConferenceKey}',
                  http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# registers API
api = endpoints.api_server([ConferenceApi]) 
