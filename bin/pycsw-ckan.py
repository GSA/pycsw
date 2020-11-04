#!/usr/bin/env python

import configparser
import getopt
import json
import logging
import sys
import datetime
import requests
import time
import io
import os

from jinja2 import Environment, FileSystemLoader
from lxml import etree
from pycsw.core import admin, config, repository, metadata, util
from sqlalchemy import Table, Boolean, Column, Text, MetaData, \
    create_engine, delete, exists, select
from sqlalchemy.exc import OperationalError, IntegrityError

from sqlalchemy.orm.session import Session

logging.basicConfig(format='%(asctime)s [%(name)s] %(levelname)s: %(message)s', level=logging.DEBUG)
log = logging.getLogger('pycsw-ckan')

CONTEXT = config.StaticContext()


def usage():
    """Provide usage instructions"""
    return '''
NAME
    pycsw-ckan.py - pycsw ckan utility

SYNOPSIS
    pycsw-ckan.py -c <command> -f <cfg> [-h]

    Available options:

    -c    Command to be performed:
              - setup_db
              - load

    -f    Filepath to pycsw configuration

    -h    Usage message

    -u    URL of CSW


EXAMPLES

    1.) setup_db: Creates repository tables and indexes

        pycsw-ckan.py -c setup_db -f default.cfg

    2.) load: Loads CKAN datasets as records into the pycsw db.

        pycsw-ckan.py -c load -f default.cfg


'''

COMMAND = None
CFG = None

if len(sys.argv) == 1:
    print(usage())
    sys.exit(1)

try:
    OPTS, ARGS = getopt.getopt(sys.argv[1:], 'c:f:ho:p:ru:x:s:t:y')
except getopt.GetoptError as err:
    print('\nERROR: %s' % err)
    print(usage())
    sys.exit(2)

for o, a in OPTS:
    if o == '-c':
        COMMAND = a
    if o == '-f':
        CFG = a
    if o == '-h':  # dump help and exit
        print(usage())
        sys.exit(3)

SCP = configparser.SafeConfigParser()
SCP.readfp(open(CFG))

DATABASE = SCP.get('repository', 'database')
URL = SCP.get('server', 'url')
METADATA = dict(SCP.items('metadata:main'))
try:
    TABLE = SCP.get('repository', 'table')
except configparser.NoOptionError:
    TABLE = 'records'
CKAN_URL = SCP.get('defaults', 'ckan_url')


def to_date(unixtime):
    """Convert unix time to YYYY-MM-DD"""
    try:
        return datetime.datetime.fromtimestamp(unixtime).strftime('%Y-%m-%d')
    except ValueError:
        return '-1'


def keyword_list(value):
    """Ensure keywords are treated as lists"""
    if isinstance(value, list):  # list already
        return value
    else:  # csv string
        return value.split(',')


def get_record(client, context, repo, ckan_url, ckan_id, ckan_info):

    query = ckan_url + '/harvest/object/%s'
    url = query % ckan_info['harvest_object_id']
    log.debug('Fetching url=%s', url)
    response = client.get(url)

    if not response.ok:
        log.error('Could not get Harvest object for id=%s (%d: %s)',
                  ckan_id, response.status_code, response.reason)
        return

    if ckan_info['source'] in ['arcgis', 'datajson']:  # convert json to iso
        result = response.json()
        tmpldir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..',
                               'pycsw/templates')
        env = Environment(loader=FileSystemLoader(tmpldir))

        if ckan_info['source'] == 'arcgis':
            log.debug('ArcGIS detected. Converting ArcGIS JSON to ISO XML: %s', ckan_id)
            env.filters['to_date'] = to_date
            tmpl = 'arcgisjson2iso.xml'
        else:
            log.debug('Open Data JSON detected. Converting to ISO XML: %s', ckan_id)
            env.filters['keyword_list'] = keyword_list
            tmpl = 'datajson2iso.xml'

        template = env.get_template(tmpl)
        content = template.render(json=result)

    else:  # harvested ISO XML
        content = response.content

    # from here we have an ISO document no matter what
    try:
        try:
            log.debug('parsing XML as is')
            xml = etree.parse(io.BytesIO(content))
        except:
            log.debug('parsing XML with .encode("utf8")')
            xml = etree.parse(io.BytesIO(content.encode("utf8")))
    except Exception as err:
        log.exception('Could not pass xml doc from %s', ckan_id)
        return

    try:
        log.debug('Parsing ISO XML')
        record = metadata.parse_record(context, xml, repo)[0]
        if not record.identifier:  # force id into ISO XML
            log.debug('gmd:fileIdentifier is empty. Inserting id=%s', ckan_id)

            record.identifier = ckan_id

            gmd_ns = 'http://www.isotc211.org/2005/gmd'
            gco_ns = 'http://www.isotc211.org/2005/gco'

            xname = xml.find('{%s}fileIdentifier' % gmd_ns)
            if xname is None:  # doesn't exist, insert it
                log.debug('Inserting new gmd:fileIdentifier')
                fileid = etree.Element('{%s}fileIdentifier' % gmd_ns)
                etree.SubElement(fileid, '{%s}CharacterString' % gco_ns).text = ckan_id
                xml.insert(0, fileid)
            else:  # gmd:fileIdentifier exists, check for gco:CharacterString
                log.debug('Updating')
                value = xname.find('{%s}CharacterString' % gco_ns)
                if value is None:
                    log.debug('missing gco:CharacterString')
                    etree.SubElement(xname, '{%s}CharacterString' % gco_ns).text = ckan_id
                else:
                    log.debug('empty gco:CharacterString')
                    value.text = ckan_id
            record.xml = etree.tostring(xml)

    except Exception as err:
        log.exception('Could not extract metadata from %s', ckan_id)
        return

    record.ckan_id = ckan_id
    record.ckan_modified = ckan_info['metadata_modified']
    record.ckan_collection = ckan_info['ckan_collection']
    if 'collection_package_id' in ckan_info:
        record.parentidentifier = ckan_info['collection_package_id']

    return record


if COMMAND is None:
    print('-c <command> is a required argument')
    sys.exit(4)

if COMMAND not in ['setup_db', 'load', 'set_keywords']:
    print('ERROR: invalid command name: %s' % COMMAND)
    sys.exit(5)

if COMMAND == 'setup_db':
    ckan_columns = [
        Column('ckan_id', Text, index=True),
        Column('ckan_modified', Text),
        Column('ckan_collection', Boolean, index=True),
    ]
    try:
        admin.setup_db(DATABASE, TABLE,
                home='',
                create_plpythonu_functions=False,
                extra_columns=ckan_columns)
    except Exception as err:
        print(err)
        print('ERROR: DB creation error.  Database tables already exist')
        print('Delete tables or database to reinitialize')

    # create system_info table
    engine = create_engine(DATABASE)
    mdata = MetaData(engine, schema=None)

    log.info('Creating system info table')
    system_info = Table(
        'system_info', mdata,
        Column('key', Text, index=True),
        Column('value', Text, index=True),
    )

    try:
        system_info.create()
        engine.execute("INSERT INTO system_info VALUES('keywords', '');")
    except Exception as err:
        print(err)
        print('ERROR: DB creation error.  Database tables already exist')

elif COMMAND == 'load':
    engine = create_engine(DATABASE)
    session = Session(bind=engine, autocommit=True)
    mdata = MetaData(engine, schema=None)
    # Maybe this should be part of setup_db
    ckan_load = Table('ckan_load', mdata,
        Column('ckan_id', Text, primary_key=True),
        Column('ckan_modified', Text, index=True),
    )
    if not ckan_load.exists():
        ckan_load.create()

    repo = repository.Repository(DATABASE, CONTEXT, table=TABLE)

    # Configure retries for CKAN API in case of a network hiccup
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    ckan_api = requests.Session()
    ckan_api.mount('https://', adapter)

    # Set the auth_tkt cookie to talk to admin API
    ckan_api.cookies = requests.cookies.cookiejar_from_dict(dict(auth_tkt='1'))

    def __reconcile(gathered_records, existing_records):
        """
        Compares the gathered_records against existing ones.
        """

        new = set(gathered_records) - set(existing_records)
        changed = set()

        for key in set(gathered_records) & set(existing_records):
            if gathered_records[key]['metadata_modified'] > existing_records[key]:
                changed.add(key)

        log.info('Reconciling new records count=%s', len(new))
        for ckan_id in new:
            ckan_info = gathered_records[ckan_id]
            record = get_record(ckan_api, CONTEXT, repo, CKAN_URL, ckan_id, ckan_info)
            if not record:
                # Is this a potential error?
                log.warning('Skipped record %s', ckan_id)
                continue
            try:
                repo.insert(record, 'local', util.get_today_and_now())
                log.debug('Inserted %s', ckan_id)
            except (OperationalError, RuntimeError):
                log.exception('Failed to insert %s', ckan_id)
                continue
            except (IntegrityError):
                log.exception('Duplicate GUID, failed to insert %s', ckan_id)
                continue

        log.info('Reconciling updated records count=%s', len(changed))
        for ckan_id in changed:
            ckan_info = gathered_records[ckan_id]
            record = get_record(ckan_api, CONTEXT, repo, CKAN_URL, ckan_id, ckan_info)
            if not record:
                # Error?
                log.warning('Skipped record %s', ckan_id)
                continue
            update_dict = dict([(getattr(repo.dataset, key),
            getattr(record, key)) \
            for key in record.__dict__.keys() if key != '_sa_instance_state'])
            try:
                repo.session.begin()
                repo.session.query(repo.dataset).filter_by(
                ckan_id=ckan_id).update(update_dict)
                repo.session.commit()
                log.debug('Changed %s', ckan_id)
            except RuntimeError:
                log.exception('Failed to update ckan_id=%s', ckan_id)
                repo.session.rollback()
                continue

        log.info('Reconciled summary gathered=%s new=%s changed=%s',
                 len(gathered_records), len(new), len(changed))


    # Check for an interrupted load and continue where it left off.
    log.info('Checking for an interrupted load job to resume')
    last_processed_metadata_modified = None
    with session.begin():
        # This could be None if there is no interrupted load job
        last_processed_metadata_modified = (session.
            query(ckan_load.c.ckan_modified).order_by(ckan_load.c.ckan_modified.desc()).first())
        if last_processed_metadata_modified:
            log.info('Resuming from previous load job metadata_modified=%s', last_processed_metadata_modified)

    log.info('Gathering CKAN datasets identifiers in batches')
    start = 0
    error_count = 0
    while True:
        log.info('Fetching records from start=%s', start)
        gathered_records = {}

        # It's important that we process the datasets in order from oldest to
        # newest. We assume that when a dataset is updated, it's
        # metadata_modified is modified. If we are interrupted and need to
        # pick up where we left off, we can be sure that everything up to
        # last_processed_metadata_modified has been synced and can continue
        # from that last timestamp. Note: datasets older than that could have
        # been deleted, but we will pick that up in the next clean load job.
        #
        # TODO this is a legacy API and should move to the action/pacage_search
        # API. We need to handle collection members properly, since CKAN
        # filters out collection_dataset_id unless it is specified.
        response = ckan_api.get('%s/api/search/dataset' % CKAN_URL, params={
            'qjson': json.dumps({
                'fl': ','.join([
                    'id',
                    'metadata_modified',
                    'extras_harvest_object_id',
                    'extras_source_datajson_identifier',
                    'extras_metadata_source',
                    'extras_collection_package_id',
                ]),
                'q': '+harvest_object_id:* +metadata_modified:[%s TO *]' % (last_processed_metadata_modified or '*'),
                'limit': 1000,
                'start': start,
                'sort': 'metadata_modified asc',
            })
        })

        if not response.ok:
            log.error('CKAN API responded status=%s reason=%s', response.status_code, response.reason)
            error_count += 1
            if error_count > 2:
                log.error('Exiting after multiple errors count=%s', error_count)
            time.sleep(20 * error_count)
            # Try again
            continue

        listing = response.json()
        if not isinstance(listing, dict):
            raise RuntimeError('Wrong API response: %s' % listing)
        results = listing.get('results')
        if not results:
            # We're done!
            break

        # At this point, increment the offset so our next request is the next batch
        start += 1000

        # Check that we are not resuming a run and need to skip already processed datasets
        already_processed = set()
        if last_processed_metadata_modified:
            with session.begin():
                already_processed = set([r.ckan_id for r in session.execute(select([ckan_load.c.ckan_id]).where(ckan_load.c.ckan_id.in_(result['id'] for result in results)))])
                log.info('Some gathered datasets already processed already_processed=%s gathered=%s', len(already_processed), len(results))

        for result in (r for r in results if r['id'] not in already_processed):
            gathered_records[result['id']] = {
                'metadata_modified': result['metadata_modified'],
                'harvest_object_id': result['extras']['harvest_object_id'],
                'source': result['extras'].get('metadata_source')
            }

            # You are not a collection if you are a member of a collection.
            # Members will have the collection_package_id
            is_collection = 'collection_package_id' not in result['extras']
            if not is_collection:
                gathered_records[result['id']]['collection_package_id'] = \
                    result['extras']['collection_package_id']

            gathered_records[result['id']]['ckan_collection'] = is_collection
            if 'source_datajson_identifier' in result['extras']:
                gathered_records[result['id']]['source'] = 'datajson'

        if not gathered_records:
           # Everything gathered has already been processed, get a new batch
           continue

        # Fetch existing records, if any
        existing_records = {}
        query = (repo.session
            .query(repo.dataset.ckan_id, repo.dataset.ckan_modified)
            .filter(repo.dataset.ckan_id.in_(gathered_records.keys())))
        for row in query:
            existing_records[row.ckan_id] = row.ckan_modified
        log.info('Found count=%s existing datasets', len(existing_records.keys()))

        # Reconcile what's been fetched
        __reconcile(gathered_records, existing_records)

        # Insert rows into ckan_load so we know what we've processed. Interrupt the job if this fails.
        with session.begin():
            session.execute(ckan_load.insert().values([(ckan_id, gathered_records[ckan_id]['metadata_modified']) for ckan_id in gathered_records.keys()]))

        log.info('Reconciled CKAN datasets progress=%s', start)

    with session.begin():
        # Next, delete any records that we didn't see while scanning CKAN. We let
        # errors raise and interrupt the job since it puts into question the
        # integrity of our sync.
        log.info('Deleting records not seen')
        deleted_count = session.execute(delete(repo.dataset, ~exists().where(repo.dataset.ckan_id == ckan_load.c.ckan_id))).rowcount
        log.info('Deleted records count=%s', deleted_count)

        # Truncate ckan_load. Let errors raise and interrupt.
        log.info('Cleaning up')
        session.execute('TRUNCATE TABLE ckan_load')


elif COMMAND == 'set_keywords':
    """set pycsw service metadata keywords from top limit CKAN tags"""
    limit = 20

    print('Fetching tags from %s' % CKAN_URL)
    url = CKAN_URL + '/api/action/package_search?facet.field=["tags"]&rows=0&facet.limit=%s' % limit
    response = requests.get(url)
    tags = response.json()['result']['facets']['tags']

    print('Deriving top %d tags' % limit)
    # uniquify and sort by top limit
    tags_sorted = sorted(tags, key=tags.__getitem__, reverse=True)
    tags_trimmed = [x for x in tags_sorted if x != '']
    keywords = ','.join(tags_trimmed)

    from sqlalchemy import create_engine
    engine = create_engine(DATABASE)
    engine.execute("UPDATE system_info SET value='%s' WHERE key='keywords'" % keywords)
    print('Tags saved to system_info table.')

print('Done')
