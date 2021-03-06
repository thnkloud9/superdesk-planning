# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Superdesk Events"""

import superdesk
import logging
from superdesk import get_resource_service
from superdesk.metadata.utils import generate_guid
from superdesk.metadata.item import GUID_NEWSML
from superdesk.notification import push_notification
from apps.archive.common import set_original_creator, get_user
from .common import STATE_SCHEMA
from dateutil.rrule import rrule, YEARLY, MONTHLY, WEEKLY, DAILY, MO, TU, WE, TH, FR, SA, SU
from eve.defaults import resolve_default_values
from eve.methods.common import resolve_document_etag
from eve.utils import config
from flask import current_app as app
import itertools
import copy
import pytz
import re

logger = logging.getLogger(__name__)

not_analyzed = {'type': 'string', 'index': 'not_analyzed'}
not_indexed = {'type': 'string', 'index': 'no'}

FREQUENCIES = {'DAILY': DAILY, 'WEEKLY': WEEKLY, 'MONTHLY': MONTHLY, 'YEARLY': YEARLY}
DAYS = {'MO': MO, 'TU': TU, 'WE': WE, 'TH': TH, 'FR': FR, 'SA': SA, 'SU': SU}

organizer_roles = {
    'eorol:artAgent': 'Artistic agent',
    'eorol:general': 'General organiser',
    'eorol:tech': 'Technical organiser',
    'eorol:travAgent': 'Travel agent',
    'eorol:venue': 'Venue organiser'
}


class EventsService(superdesk.Service):
    """Service class for the events model."""

    def post_in_mongo(self, docs, **kwargs):
        for doc in docs:
            resolve_default_values(doc, app.config['DOMAIN'][self.datasource]['defaults'])
        self.on_create(docs)
        resolve_document_etag(docs, self.datasource)
        ids = self.backend.create_in_mongo(self.datasource, docs, **kwargs)
        self.on_created(docs)
        return ids

    def patch_in_mongo(self, id, document, original):
        res = self.backend.update_in_mongo(self.datasource, id, document, original)
        return res

    def set_ingest_provider_sequence(self, item, provider):
        """Sets the value of ingest_provider_sequence in item.

        :param item: object to which ingest_provider_sequence to be set
        :param provider: ingest_provider object, used to build the key name of sequence
        """
        sequence_number = get_resource_service('sequences').get_next_sequence_number(
            key_name='ingest_providers_{_id}'.format(_id=provider[config.ID_FIELD]),
            max_seq_number=app.config['MAX_VALUE_OF_INGEST_SEQUENCE']
        )
        item['ingest_provider_sequence'] = str(sequence_number)

    def on_create(self, docs):
        # events generated by recurring rules
        generatedEvents = []
        for event in docs:
            # generates an unique id
            if 'guid' not in event:
                event['guid'] = generate_guid(type=GUID_NEWSML)
            event['_id'] = event['guid']
            # set the author
            set_original_creator(event)

            # overwrite expiry date
            overwrite_event_expiry_date(event)

            # generates events based on recurring rules
            if event['dates'].get('recurring_rule', None):
                # generate a common id for all the events we will generate
                setRecurringMode(event)
                recurrence_id = generate_guid(type=GUID_NEWSML)
                # compute the difference between start and end in the original event
                time_delta = event['dates']['end'] - event['dates']['start']
                # for all the dates based on the recurring rules:
                for date in itertools.islice(generate_recurring_dates(
                    start=event['dates']['start'],
                    tz=event['dates'].get('tz') and pytz.timezone(event['dates']['tz'] or None),
                    **event['dates']['recurring_rule']
                ), 0, 200):  # set a limit to prevent too many events to be created
                    # create event with the new dates
                    new_event = copy.deepcopy(event)
                    new_event['dates']['start'] = date
                    new_event['dates']['end'] = date + time_delta
                    # set a unique guid
                    new_event['guid'] = generate_guid(type=GUID_NEWSML)
                    new_event['_id'] = new_event['guid']
                    # set the recurrence id
                    new_event['recurrence_id'] = recurrence_id

                    # set expiry date
                    overwrite_event_expiry_date(new_event)

                    generatedEvents.append(new_event)
                # remove the event that contains the recurring rule. We don't need it anymore
                docs.remove(event)
        if generatedEvents:
            docs.extend(generatedEvents)

    def on_created(self, docs):
        """Send WebSocket Notifications for created Events

        Generate the list of IDs for recurring and non-recurring events
        Then send this list off to the clients so they can fetch these events
        """
        notifications_sent = []

        for doc in docs:
            event_type = 'events:created'
            event_id = str(doc.get(config.ID_FIELD))
            user_id = str(doc.get('original_creator', ''))

            if doc.get('recurrence_id'):
                event_type = 'events:created:recurring'
                event_id = str(doc['recurrence_id'])

            # Don't send notification if one has already been sent
            # This is to ensure recurring events to send multiple notifications
            if event_id in notifications_sent:
                continue

            notifications_sent.append(event_id)
            push_notification(
                event_type,
                item=event_id,
                user=user_id
            )

    def on_update(self, updates, original):
        if 'skip_on_update' in updates:
            # this is an recursive update(see below)
            del updates['skip_on_update']
            return

        user = get_user()
        if user and user.get(config.ID_FIELD):
            updates['version_creator'] = user[config.ID_FIELD]

        # The rest of this update function expects 'dates' to be in updates
        # This can cause issues, as a workaround for now add the dictionary in manually
        # Until a better fix can be implemented
        if 'dates' not in updates:
            updates['dates'] = {}

        if not updates['dates'].get('recurring_rule', None):
            # we keep the orignal and set it as not recursive
            updates['dates']['recurring_rule'] = None
            updates['recurrence_id'] = None
            # we spike all the related recurrent events
            if 'recurrence_id' in original:
                # retieve all the related events
                events = self.find(where={
                    # all the events created from the same rec rules
                    'recurrence_id': original['recurrence_id'],
                    # except the original
                    '_id': {'$ne': original['_id']},
                    # only future ones
                    'dates.start': {'$gt': original['dates']['start']},
                })
                spike_service = get_resource_service('events_spike')
                # spike them
                for event in events:
                    spike_service.patch(event[config.ID_FIELD], {})
            push_notification(
                'events:updated',
                item=str(original[config.ID_FIELD]),
                user=str(updates.get('version_creator', ''))
            )
            return

        # update all following events
        setRecurringMode(updates)
        updates['recurrence_id'] = original.get('recurrence_id', None) or generate_guid(type=GUID_NEWSML)
        # get the list of all items that follows the current edited one
        if not original['dates'].get('recurring_rule', None):
            existingEvents = [original]
        else:
            existingEvents = self.find(where={'recurrence_id': updates['recurrence_id']})
            existingEvents = [
                event for event in existingEvents
                if event['dates']['start'] >= original['dates']['start']
            ]
        # compute the difference between start and end in the original event
        time_delta = updates['dates']['end'] - updates['dates']['start']
        addEvents = []
        # generate the dates for the following events
        dates = [date for date in itertools.islice(generate_recurring_dates(
            start=updates['dates']['start'],
            tz=updates['dates'].get('tz') and pytz.timezone(updates['dates']['tz'] or None),
            **updates['dates']['recurring_rule']
        ), 0, 200)]

        for event, date in itertools.zip_longest(existingEvents, dates):
            if not date:
                # date is not present so the current event should be deleted
                self.delete({'_id': event['_id']})
                get_resource_service('events_history').on_item_deleted(event)
            elif not event:
                # the event is not present so a new event should be created
                new_event = copy.deepcopy(original)
                new_updates = copy.deepcopy(updates)
                new_event.update(new_updates)
                new_event['dates']['start'] = date
                new_event['dates']['end'] = date + time_delta
                # set a unique guid
                new_event['guid'] = generate_guid(type=GUID_NEWSML)
                new_event['_id'] = new_event['guid']
                # set the recurrence id
                addEvents.append(new_event)
            elif event['_id'] == original['_id']:
                updates['dates']['start'] = date
                updates['dates']['end'] = date + time_delta
            else:
                # update the event with the new date and new updates
                new_updates = copy.deepcopy(updates)
                if 'guid' in new_updates:
                    del new_updates['guid']
                new_updates['dates']['start'] = date
                new_updates['dates']['end'] = date + time_delta
                new_updates['skip_on_update'] = True
                # set the recurrence id
                self.patch(event['_id'], new_updates)

        if addEvents:
            # add all new events
            self.create(addEvents)
            get_resource_service('events_history').on_item_created(addEvents)

        push_notification(
            'events:updated:recurring',
            item=str(original[config.ID_FIELD]),
            recurrence_id=str(updates['recurrence_id']),
            user=str(updates.get('version_creator', ''))
        )


events_schema = {
    # Identifiers
    '_id': {'type': 'string', 'unique': True},
    'guid': {
        'type': 'string',
        'unique': True,
        'mapping': not_analyzed
    },
    'unique_id': {
        'type': 'integer',
        'unique': True,
    },
    'unique_name': {
        'type': 'string',
        'unique': True,
        'mapping': not_analyzed
    },
    'version': {
        'type': 'integer'
    },
    'ingest_id': {
        'type': 'string',
        'mapping': not_analyzed
    },
    'recurrence_id': {
        'type': 'string',
        'mapping': not_analyzed,
        'nullable': True,
    },

    # Audit Information
    'original_creator': superdesk.Resource.rel('users', nullable=True),
    'version_creator': superdesk.Resource.rel('users'),
    'firstcreated': {
        'type': 'datetime'
    },
    'versioncreated': {
        'type': 'datetime'
    },

    # Ingest Details
    'ingest_provider': superdesk.Resource.rel('ingest_providers'),
    'source': {     # The value is copied from the ingest_providers vocabulary
        'type': 'string'
    },
    'original_source': {    # This value is extracted from the ingest
        'type': 'string',
        'mapping': not_analyzed
    },
    'ingest_provider_sequence': {
        'type': 'string',
        'mapping': not_analyzed
    },
    'event_created': {
        'type': 'datetime'
    },
    'event_lastmodified': {
        'type': 'datetime'
    },
    # Event Details
    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 15.2
    'name': {
        'type': 'string',
        'required': True,
    },
    'definition_short': {'type': 'string'},
    'definition_long': {'type': 'string'},
    'anpa_category': {
        'type': 'list',
        'nullable': True,
        'mapping': {
            'type': 'object',
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed,
            }
        }
    },
    'files': {
        'type': 'list',
        'nullable': True,
        'schema': superdesk.Resource.rel('events_files'),
        'mapping': not_analyzed,
    },
    'relationships': {
        'type': 'dict',
        'schema': {
            'broader': {'type': 'string'},
            'narrower': {'type': 'string'},
            'related': {'type': 'string'}
        },
    },
    'links': {
        'type': 'list',
        'nullable': True
    },

    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 15.4.3
    'dates': {
        'type': 'dict',
        'schema': {
            'start': {'type': 'datetime'},
            'end': {'type': 'datetime'},
            'tz': {'type': 'string'},
            'duration': {'type': 'string'},
            'confirmation': {'type': 'string'},
            'recurring_date': {
                'type': 'list',
                'nullable': True,
                'mapping': {
                    'type': 'date'
                }
            },
            'recurring_rule': {
                'type': 'dict',
                'schema': {
                    'frequency': {'type': 'string'},
                    'interval': {'type': 'integer'},
                    'endRepeatMode': {'type': 'string'},
                    'until': {'type': 'datetime', 'nullable': True},
                    'count': {'type': 'integer', 'nullable': True},
                    'bymonth': {'type': 'string', 'nullable': True},
                    'byday': {'type': 'string', 'nullable': True},
                    'byhour': {'type': 'string', 'nullable': True},
                    'byminute': {'type': 'string', 'nullable': True},
                },
                'nullable': True
            },
            'occur_status': {
                'nullable': True,
                'type': 'dict',
                'mapping': {
                    'properties': {
                        'qcode': not_analyzed,
                        'name': not_analyzed
                    }
                },
                'schema': {
                    'qcode': {'type': 'string'},
                    'name': {'type': 'string'},
                }
            },
            'ex_date': {
                'type': 'list',
                'mapping': {
                    'type': 'date'
                }
            },
            'ex_rule': {
                'type': 'dict',
                'schema': {
                    'frequency': {'type': 'string'},
                    'interval': {'type': 'string'},
                    'until': {'type': 'datetime', 'nullable': True},
                    'count': {'type': 'integer', 'nullable': True},
                    'bymonth': {'type': 'string', 'nullable': True},
                    'byday': {'type': 'string', 'nullable': True},
                    'byhour': {'type': 'string', 'nullable': True},
                    'byminute': {'type': 'string', 'nullable': True}
                }
            }
        }
    },  # end dates
    'occur_status': {
        'type': 'dict',
        'schema': {
            'qcode': {'type': 'string'},
            'name': {'type': 'string'}
        }
    },
    'news_coverage_status': {
        'type': 'dict',
        'schema': {
            'qcode': {'type': 'string'},
            'name': {'type': 'string'}
        }
    },
    'registration': {
        'type': 'string'
    },
    'access_status': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'subject': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'location': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': {'type': 'string'},
                'name': {'type': 'string'},
                'geo': {'type': 'string'}
            }
        }
    },
    'participant': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'participant_requirement': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'organizer': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'event_contact_info': {
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },
    'event_language': {  # TODO: this is only placeholder schema
        'type': 'list',
        'mapping': {
            'properties': {
                'qcode': not_analyzed,
                'name': not_analyzed
            }
        }
    },

    # These next two are for spiking/unspiking and purging events
    'state': STATE_SCHEMA,
    'expiry': {
        'type': 'datetime',
        'nullable': True
    }

}  # end events_schema


class EventsResource(superdesk.Resource):
    """Resource for events data model

    See IPTC-G2-Implementation_Guide (version 2.21) Section 15.4 for schema details
    """

    url = 'events'
    schema = events_schema
    item_url = 'regex("[\w,.:-]+")'
    resource_methods = ['GET', 'POST']
    datasource = {
        'source': 'events',
        'search_backend': 'elastic',
    }
    item_methods = ['GET', 'PATCH', 'PUT']
    public_methods = ['GET']
    privileges = {'POST': 'planning_event_management',
                  'PATCH': 'planning_event_management'}


def generate_recurring_dates(start, frequency, interval=1, endRepeatMode='unlimited',
                             until=None, byday=None, count=None, tz=None):
    """

    Returns list of dates related to recurring rules

    :param start datetime: date when to start
    :param frequency str: DAILY, WEEKLY, MONTHLY, YEARLY
    :param interval int: indicates how often the rule repeats as a positive integer
    :param until datetime: date after which the recurrence rule expires
    :param byday str or list: "MO TU"
    :param count int: number of occurrences of the rule
    :return list: list of datetime

    """
    # if tz is given, respect the timzone by starting from the local time
    # NOTE: rrule uses only naive datetime
    if tz:
        try:
            # start can already be localized
            start = pytz.UTC.localize(start)
        except ValueError:
            pass
        start = start.astimezone(tz).replace(tzinfo=None)
        if until:
            until = until.astimezone(tz).replace(tzinfo=None)

    # check format of the recurring_rule byday value
    if byday and re.match(r'^-?[1-5]+.*', byday):
        # byday uses monthly or yearly frequency rule with day of week and
        # preceeding day of month intenger byday value
        # examples:
        # 1FR - first friday of the month
        # -2MON - second to last monday of the month
        if byday[:1] == '-':
            day_of_month = int(byday[:2])
            day_of_week = byday[2:]
        else:
            day_of_month = int(byday[:1])
            day_of_week = byday[1:]

        byweekday = DAYS.get(day_of_week)(day_of_month)
    else:
        # byday uses DAYS constants
        byweekday = byday and [DAYS.get(d) for d in byday.split()] or None
    # TODO: use dateutil.rrule.rruleset to incude ex_date and ex_rule
    dates = rrule(
        FREQUENCIES.get(frequency),
        dtstart=start,
        until=until,
        byweekday=byweekday,
        count=count,
        interval=interval,
    )
    # if a timezone has been applied, returns UTC
    if tz:
        return (tz.localize(dt).astimezone(pytz.UTC).replace(tzinfo=None) for dt in dates)
    else:
        return (date for date in dates)


def setRecurringMode(event):
    endRepeatMode = event.get('dates', {}).get('recurring_rule', {}).get('endRepeatMode')
    if endRepeatMode == 'unlimited':
        event['dates']['recurring_rule']['count'] = None
        event['dates']['recurring_rule']['until'] = None
    elif endRepeatMode == 'count':
        event['dates']['recurring_rule']['until'] = None
    elif endRepeatMode == 'until':
        event['dates']['recurring_rule']['count'] = None


def overwrite_event_expiry_date(event):
    if 'expiry' in event:
        event['expiry'] = event['dates']['end']
