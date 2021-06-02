#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''
  wk_load_media.py
  ================

    Parameters

        wk_load_media.py [paramaters] venue

        venue               Venue-id, e.g. ycba for Yale
        -c, --count         Process a limited number of works
        -f, --filter        Only process artwork with matching Smartify id
        --filter-category   Only process artworks with matching Smartify category
        --instance          Smartify database instance, i.e. prod or uat
        --locale            Locale, e.g. en-GB
        --no-image-upload   Don't add P4765 attribute to Wikidata item (Commons image
                            for upload). This allows the image to be added manually
                            later or linked to an existing Commons entry with P18.
        -t, --trial         Test run that just displays data to be passed to
                            artdatabot.py code. No upload to Wikidata.
        -u, --update        Update existing items. Must be used with --filter. Adds
                            statements to existing Wikidata items. Use carefully. Use
                            with --trial to see meta-data for one upload.

    Upload one item:
        wk_load_media.py --filter YCBA_B1981_25_51 ycba

    Upload 50 Drawings
        wk_load_media.py -c 50 --filter-category Drawing ycba

    Output trial meta-data for one work:
        wk_load_media.py -t -u --filter YCBA_B1981_25_51 ycba

'''

import sys
import os
import re
import argparse
import json
from json.decoder import JSONDecodeError

# pywikibot imports
import wk_artdatabot as artdatabot
import pywikibot

# Smartify imports
import sm_helpers as sm
from sm_db import SmDb
from sm_category import SmCategory

# Global variables
g_args = None
g_collection_short_name = 'YCBA'
g_collection_qid = None             # Wikidata Qid for the collection, e.g. Q6352575 for YCBA
g_accession_pid = 'P217'            # Wikidata property containing accession number


def main():
    global g_args
    global g_collection_qid

    # Get command line arguments
    parser = argparse.ArgumentParser(description='Load data into Wikimedia Commons and Wikidata')
    parser.add_argument('venue', help='Gallery venue-id (e.g. ycba)')
    parser.add_argument('-c', '--count', help='Only process limited number of artworks given by parameter', type=int, default=999999)
    parser.add_argument('-f', '--filter', help='Only process artworks with matching ids', metavar=('artwork-id'))
    parser.add_argument('--filter-category', help='Only process artworks of a given category', metavar=('category'))
    parser.add_argument('--instance', help='Smartify database instance, prod or uat')
    parser.add_argument('--locale', help='Locale, e.g. en-GB', default='en-GB')
    parser.add_argument('--no-image-upload', help="Don't load the image", action='store_true')
    parser.add_argument('-t', '--trial', help='Trial to print data loaded to artdatabot, no uploading', action='store_true')
    parser.add_argument('-u', '--update', help='Update existing works, use with --filter', action='store_true')
    g_args = parser.parse_args()

    # Get location for config file and data
    sroot = sm.get_env_var('SMARTIFY_ROOT', 'Environment variable SMARTIFY_ROOT not set')

    # Don't allow updating of existing works unless we are doing just a few.
    if g_args.update and not g_args.filter:
        print(f'ERROR: No artworks for filter {g_args.filter}')
        sys.exit(1)

    # Which database are we talking to?
    if g_args.instance:
        instance = g_args.instance
    else:
        instance = sm.get_env_var('SMARTIFY_INSTANCE', "ERROR: Smartify instance 'prod' or 'uat' not defined")
    instance = instance.lower().strip()
    smdb = SmDb(instance)
    categories = SmCategory()

    '''
    Get the pywikibot settings
    '''

    config = json.load(open(os.path.join(sroot, 'sm_config.json')))
    try:
        commons_site = config[instance]['pywikibot']['commons']
        wikidata_site = config[instance]['pywikibot']['wikidata']
    except KeyError:
        commons_site = None
        wikidata_site = None
    if not commons_site or not wikidata_site:
        print('ERROR: The two pywikibot sites have not been configured correctly')
        sys.exit(1)

    # Save other settings
    g_collection_qid = check_venue(smdb, g_args.venue)
    venue = g_args.venue.lower()
    locale = g_args.locale
    language = categories.get_language(locale)

    '''
    Load artworks and artists files, use filter if supplied
    '''

    if g_args.filter:
        artworks = smdb.get_artworks(venue, filter=g_args.filter, image=True, pretty=True)
        if not artworks:
            print(f'ERROR: No artworks for filter {g_args.filter}')
            sys.exit(1)
        artist_id = list(artworks.values())[0]['artistId']
        artists = smdb.get_artists(filter=artist_id)
    else:
        artworks = smdb.get_artworks(venue, image=True, pretty=True)
        artists = smdb.get_artists(venue, master=True)

    '''
    Get list of non-Smartify existing works in Commons.
    This list was produced using wk_category_list.py
    '''

    try:
        path = sm.get_list_path(venue, 'commons_existing.json')
        with open(path) as f:
            commons_existing = json.load(f)
    except FileNotFoundError:
        print(f'Missing JSON file [{path}]')
        sys.exit(1)
    except JSONDecodeError:
        print(f'Corrupt JSON file [{path}]')
        sys.exit(1)

    '''
    Get list of works with small images (< 20K).
    These won't be loaded into Wikidata or Commons
    '''

    try:
        path = sm.get_list_path(venue, 'small_images.json')
        with open(path) as f:
            small_images = json.load(f)
    except FileNotFoundError:
        print(f'Missing JSON file [{path}]')
        sys.exit(1)
    except JSONDecodeError:
        print(f'Corrupt JSON file [{path}]')
        sys.exit(1)

    '''
    Get list of existing Smartify works in Wikidata
    '''
    wikidata_existing = get_existing()

    '''
    Process the artworks
    '''

    # Create generator for Wikidata artworks
    dict_gen = get_ycba_generator(
        artists,
        artworks,
        wikidata_existing,
        commons_existing,
        small_images,
        categories,
        locale,
        language,
        g_args.count
    )

    if g_args.trial:
        for artwork in dict_gen:
            print(json.dumps(artwork, indent=4))
    else:
        # Initialise bot for upload with our generator. Most common cause for
        # failure is that the generator is empty which causes a StopIteration
        # exception when the artdatabot code fails to read the first item
        try:
            artDataBot = artdatabot.ArtDataBot(dict_gen, create=not g_args.update)
        except StopIteration:
            return
        artDataBot.run()

# -----------------------------------------------------------------------


def get_ycba_generator(artists, artworks, wikidata_existing, commons_existing, small_images, categories, locale, language, count):

    """
    Generator to return YCBA artworks from Smartify database
    """

    for artwork in artworks.values():

        metadata = {}
        artwork_id = artwork['artworkId']

        # ----------------------------------------------------------------------------------

        '''
        Check a bunch of things that will stop us proceding with this artwork
        '''

        # Skip artwork if it has a small image
        if artwork_id in small_images:
            print(f'WARNING: Skipping artwork [{artwork_id}] with image < 20K')
            continue

        # Skip artworks that already exist in Wikidata, unless updating
        if not g_args.update:
            if artwork['accessionNumber'] in wikidata_existing:
                print(f"WARNING: Skipping, artwork already exists in Wikidata [{artwork['accessionNumber']}]")
                continue

        # Skip artworks that are not public domain
        try:
            description = artwork['description'][locale]
        except KeyError:
            description = ''
        if 'free to use' not in description:
            print(f"WARNING: Skipping, artwork is not public domain [{artwork_id}]")
            continue

        # Set up links to Yale site and Smartify
        try:
            url = artwork['websites'][0]['url'][locale]
        except (KeyError, IndexError):
            print(f"WARNING: Skipping artwork because we don't have a URL [{artwork_id}]")
            continue
        metadata['url'] = url

        try:
            pretty_id = artwork['prettyId'][locale]
        except (KeyError, IndexError):
            print(f"WARNING: Skipping artwork because we don't have a prettyId [{artwork_id}]")
            continue
        smartify_url = f"https://smartify.org/artworks/{pretty_id}"
        metadata['describedbyurl'] = [url, smartify_url]

        # Get Qid(s) for category
        category = artwork['category']
        if category == 'Miscellaneous':
            print(f"WARNING: Skipping artwork because category is Miscellaneous [{artwork_id}]")
            continue
        category_qids = categories.get_category_qids(category)
        if not category_qids:
            print(f"WARNING: Skipping artwork because we don't have a category Qid [{artwork_id}]")
            continue

        # Skip artwork if already exists in Commons. This can either be because the work has the
        # same accession number or because it has the same TMS number. TMS numbers are used in
        # the URLs of Yale's web pages and were used by some (but not all) of Google's submission
        # in 2012.
        acc = artwork['accessionNumber']
        existing = commons_existing.get(acc)
        if existing:
            print(f"WARNING: Skipping artwork [{artwork_id}], [{acc}] already exists in Commons here [{existing['url']}]")
            continue

        tms_number = None
        try:
            # Get the Yale URL for the work
            url = artwork['websites'][0]['url'][locale]
            s = re.search(r'^.*tms:(\d+)$', url)
            if s:
                tms_number = s.group(1)
        except (KeyError, ValueError):
            pass

        if tms_number:
            existing = commons_existing.get(tms_number)
            if existing:
                print(f"WARNING: Skipping artwork [{artwork_id}], tms [{tms_number}] already exists in Commons here [{existing['url']}]")
                continue

        # Skip if a category has been specified on the command
        # line and this work is not in the category
        if g_args.filter_category:
            category = artwork['category']
            if not re.search(fr'^{g_args.filter_category}$', category):
                print(f"WARNING: Skipping artwork [{artwork_id}], incorrect category [{category}]")
                continue

        # ----------------------------------------------------------------------------------

        # Set up collection details
        metadata['collectionqid'] = g_collection_qid
        metadata['collectionshort'] = g_collection_short_name
        metadata['locationqid'] = g_collection_qid

        # Establish category of object (P31) may have more than one
        metadata['instanceofqid'] = category_qids

        # Set title
        title = artwork['title'][locale]
        title = title[0:200]
        metadata['title'] = {language: title}

        # Set accession number
        metadata['idpid'] = g_accession_pid
        metadata['id'] = artwork['accessionNumber']

        # Get the artist's name and any override like 'probably by Rembrandt'
        artist = artists[artwork['artistId']]
        artist_name = artist['name'][locale]
        try:
            artwork_artist_name = artwork['artistName'][locale]
        except KeyError:
            artwork_artist_name = artist_name

        # If the artist is anonymous, set explicitly
        if artist['artistId'] in ('MASTER_ArtistUnk', 'MASTER_MakerUnk'):
            metadata['creatorqid'] = 'Q4233718'
            metadata['creatorname'] = 'anonymous'
            metadata['description'] = {language: get_description(artwork, 'anonymous artist or maker')}
        else:
            # Get the artist's qid. If we don't have one skip (for now)
            qid = artist.get('artistQid')
            if not qid or not re.search(r'Q\d+', qid):
                print(f"WARNING: Skipping, we don't know the artist's Qid, artwork [{artwork_id}], artist {artist['artistId']}")
                continue
            # Put artist details in metadata
            metadata['creatorqid'] = qid
            metadata['creatorname'] = artist_name

            # Construct sensible description for artwork (e.g. painting by Rembrandt)
            metadata['description'] = {language: get_description(artwork, artist_name, artwork_artist_name)}

        # Extract artwork dates
        # TODO: Review forms of dates in data
        try:
            date = artwork['date'][locale]
        except KeyError:
            date = ''
        if date:
            dateregex = r'^(\d\d\d\d)$'
            datecircaregex = r'^(c\.|circa)\s*(\d\d\d\d)$'
            periodregex = r'^(\d\d\d\d)\s*-\s*(\d\d\d\d)$'
            betweenperiodregex = r'^between\s*(\d\d\d\d)\s*and\s*(\d\d\d\d)$'
            afterregex = r'^after\s*(\d\d\d\d)$'
            shortperiodregex = r'^(\d\d)(\d\d)\s*-\s*(\d\d)$'
            circaperiodregex = r'^c\.\s*(\d\d\d\d)\s*-\s*(\d\d\d\d)$'
            circashortperiodregex = r'^c\.\s*(\d\d)(\d\d)\s*-\s*(\d\d)$'

            datematch = re.search(dateregex, date)
            datecircamatch = re.search(datecircaregex, date)
            periodmatch = re.search(periodregex, date)
            betweenperiodmatch = re.search(betweenperiodregex, date, flags=re.I)
            aftermatch = re.search(afterregex, date, flags=re.I)
            circaperiodmatch = re.search(circaperiodregex, date)
            shortperiodmatch = re.search(shortperiodregex, date)
            circashortperiodmatch = re.search(circashortperiodregex, date)

            if datematch:
                metadata['inception'] = int(datematch.group(1).strip())
            elif datecircamatch:
                metadata['inception'] = int(datecircamatch.group(2).strip())
                metadata['inceptioncirca'] = True
            elif periodmatch:
                metadata['inceptionstart'] = int(periodmatch.group(1))
                metadata['inceptionend'] = int(periodmatch.group(2))
            elif betweenperiodmatch:
                metadata['inceptionstart'] = int(betweenperiodmatch.group(1))
                metadata['inceptionend'] = int(betweenperiodmatch.group(2))
            elif aftermatch:
                metadata['inception'] = int(aftermatch.group(1).strip())
                metadata['inceptionafter'] = True
            elif circaperiodmatch:
                metadata['inceptionstart'] = int(circaperiodmatch.group(1))
                metadata['inceptionend'] = int(circaperiodmatch.group(2))
                metadata['inceptioncirca'] = True
            elif shortperiodmatch:
                metadata['inceptionstart'] = int('%s%s' % (shortperiodmatch.group(1), shortperiodmatch.group(2),))
                metadata['inceptionend'] = int('%s%s' % (shortperiodmatch.group(1), shortperiodmatch.group(3),))
            elif circashortperiodmatch:
                metadata['inceptionstart'] = int('%s%s' % (circashortperiodmatch.group(1), circashortperiodmatch.group(2),))
                metadata['inceptionend'] = int('%s%s' % (circashortperiodmatch.group(1), circashortperiodmatch.group(3),))
                metadata['inceptioncirca'] = True
            else:
                print(f"WARNING: Skipping, could not parse date [{date}], artwork [{artwork_id}]")
                continue

        # Then add the medium (e.g. oil on canvas) as a list of qids
        try:
            media = artwork['medium']['en-GB']
            qids = get_medium_poperties(media)
        except KeyError:
            media = ''
            qids = None
        if not qids:
            print(f"WARNING: Skipping, could not find any media for [{media}], artwork [{artwork_id}]")
            continue
        metadata['medium'] = qids

        # Set dimensions
        height = artwork.get('dimensionHeight')
        if height:
            metadata['heightcm'] = str(height)
        width = artwork.get('dimensionWidth')
        if width:
            metadata['widthcm'] = str(width)
        depth = artwork.get('dimensionDepth')
        if depth:
            metadata['depthcm'] = str(depth)

        # Set image
        image_url = artwork.get('publicUrl')
        if image_url and not g_args.no_image_upload:
            metadata['imageurl'] = image_url
            metadata['imageoperatedby'] = g_collection_qid
            metadata['imageurlformat'] = 'Q2195'            # JPEG
            metadata['imageurllicense'] = 'Q6938433'        # CC0

        # Process only requested number of works/yields

        if count > 0:
            count -= 1
            yield metadata
        else:
            return


# -----------------------------------------------------------------------


def get_description(artwork, artist_name, artwork_artist_name=None):
    # Work out some kind of description using the category and artist name.
    # E.g. painting by Rembrandt. If the artist name is something like
    # 'probably by Rembrandt' just use a hyphen to separate category and
    # name and return 'painting - probably by Rembrandt
    category = artwork['category']
    if category == 'Miscellaneous':
        category = 'artwork'
    else:
        category = category[:1].lower() + category[1:]

    artwork_artist_name = lower_case_prefixes(artwork_artist_name)
    if artwork_artist_name is None or artist_name == artwork_artist_name:
        description = f'{category} by {artist_name}'
    else:
        description = f'{category} - {artwork_artist_name}'

    return description


def lower_case_prefixes(name):
    '''
    Make first character of artist name prefixes lower-case. So:
        Probably Fred Bloggs
    becomes:
        probably Fred Bloggs
    '''

    if name:
        terms = r'(Attributed|Circle|Commenced|Copy|Designed|Drawing|Engraved|Etched|Formerly|Imitator|Landscape|Portrait|Possibly|Print|Printed|Published|Pupil|Related|Studio)[\s:]'
        s = re.search(terms, name)
        if s:
            name = name[:1].lower() + name[1:]

    return name


# -----------------------------------------------------------------------


def get_existing():
    '''
    Build an dictionary of Qids keyed by artwork's accession number
    '''

    result = {}
    sq = pywikibot.data.sparql.SparqlQuery()

    # Construct SparQL to get accession number Qids
    query = """
        SELECT ?item ?id WHERE {
        ?item p:P195/ps:P195 wd:%s .
        ?item p:%s ?statement .
        ?statement pq:P195 wd:%s .
        ?statement ps:%s ?id }
    """ % (g_collection_qid, g_accession_pid, g_collection_qid, g_accession_pid)

    sq = pywikibot.data.sparql.SparqlQuery()
    query_result = sq.select(query)
    for result_item in query_result:
        qid = result_item.get('item').replace(u'http://www.wikidata.org/entity/', u'')
        result[result_item.get('id')] = qid
    return result

# -----------------------------------------------------------------------


def get_dimensions_var(artwork):
    unit = artwork.get('dimensionUnit')
    if not unit:
        return ''
    else:
        da = ['size']
        da.append(unit)

        try:
            da.append(str(artwork['dimensionHeight']))
        except KeyError:
            pass

        try:
            da.append(str(artwork['dimensionWidth']))
        except KeyError:
            pass

        try:
            da.append(str(artwork['dimensionDepth']))
        except KeyError:
            pass
        return '{{%s}}' % '|'.join(da)

# ----------------------------------------------------------------------


def check_venue(smdb, venue_id):
    venue_id = venue_id.upper()
    venue = smdb.get_venues(venue_id)
    if not venue:
        print('ERROR: Invalid venue [{}]'.format(venue_id))
        sys.exit(1)
    try:
        qid = venue[venue_id]['collectionQid']
    except KeyError:
        print('ERROR: No Wikidata Qid for this collection [{}]'.format(venue_id))
        sys.exit(1)
    return qid

# ----------------------------------------------------------------------


def get_medium_poperties(medium_text):
    properties = {}     # Dict of Qids to return
    paint = False       # One of the initial media involved paint

    # Look for medium term in text
    for medium in media:
        if re.search(fr"(^|\W){medium['medium']}", medium_text, flags=re.I):
            medium_text = re.sub(medium['medium'], '', medium_text, flags=re.I)
            # If the medium is a type of paint, remove the word
            # paint so we don't pick up the less specific term
            if medium['paint']:
                medium_text = re.sub('paint', '', medium_text, flags=re.I)
                paint = True
            # If we don't already have this medium, add it
            if medium['qid'] not in properties:
                properties[medium['qid']] = False

    for surface in surfaces:
        on_search = fr"(^|\s)on\s+{surface['surface']}"
        if re.search(on_search, medium_text, flags=re.I):
            medium_text = re.sub(on_search, '', medium_text, flags=re.I)
            # If we have an 'on' surface where one of the
            # things put on it was paint of some form save
            # the Qid with True, if not paint then False
            properties[surface['qid']] = paint
            break
    for surface in surfaces:
        if re.search(fr"(^|\W){surface['surface']}", medium_text, flags=re.I):
            medium_text = re.sub(surface['surface'], '', medium_text, flags=re.I)
            properties[surface['qid']] = False

    return properties


'''
Media found in Yale artworks
    - Media text
    - Wikidata Qid
    - Is media a type of paint, True/False
'''
media = [
    {'medium': r'Ancaster stone', 'qid': 'Q4752538', 'paint': False},
    {'medium': r'acrylic', 'qid': 'Q207849', 'paint': True},
    {'medium': r'alabaster', 'qid': 'Q143447', 'paint': False},
    {'medium': r'albumen\s+print', 'qid': 'Q580807', 'paint': False},
    {'medium': r'aquatint', 'qid': 'Q473236', 'paint': False},
    {'medium': r'arborite', 'qid': 'Q4784911', 'paint': False},
    {'medium': r'black\s+basalt', 'qid': 'Q98097860', 'paint': False},
    {'medium': r'bronze', 'qid': 'Q34095', 'paint': False},
    {'medium': r'brown\s+ink', 'qid': 'Q58344150', 'paint': False},
    {'medium': r'ink', 'qid': 'Q127418', 'paint': False},
    {'medium': r'black\s+chalk', 'qid': 'Q3387833', 'paint': False},
    {'medium': r'red\s+chalk', 'qid': 'Q901944', 'paint': False},                   # sanguine
    {'medium': r'carborundum', 'qid': 'Q3206631', 'paint': False},
    {'medium': r'ceramic', 'qid': 'Q45621', 'paint': False},
    {'medium': r'chalk', 'qid': 'Q183670', 'paint': False},
    {'medium': r'charcoal', 'qid': 'Q1424515', 'paint': False},
    {'medium': r'chine\s+collé', 'qid': 'Q3674992', 'paint': False},
    {'medium': r'cibachrome', 'qid': 'Q1622095', 'paint': False},
    {'medium': r'collotype', 'qid': 'Q1572315', 'paint': False},
    {'medium': r'concrete', 'qid': 'Q22657', 'paint': False},
    {'medium': r'coade\s+stone', 'qid': 'Q682083', 'paint': False},
    {'medium': r'copper', 'qid': 'Q753', 'paint': False},
    {'medium': r'conté crayon', 'qid': 'Q1129270', 'paint': False},
    {'medium': r'drypoint', 'qid': 'Q542340', 'paint': False},
    {'medium': r'dye coupler', 'qid': 'Q172922', 'paint': False},                   # chromogenic dye coupler print
    {'medium': r'enamel', 'qid': 'Q213371', 'paint': False},
    {'medium': r'stipple(|\s+engraving)', 'qid': 'Q7617514', 'paint': False},
    {'medium': r'line\s+engraving', 'qid': 'Q747704', 'paint': False},
    {'medium': r'engrav', 'qid': 'Q11835431', 'paint': False},                      # engraving, engraved, etc.
    {'medium': r'etching|etched', 'qid': 'Q186986', 'paint': False},
    {'medium': r'gelatin\s+silver\s+print', 'qid': 'Q64029133', 'paint': False},
    {'medium': r'gesso', 'qid': 'Q1514256', 'paint': True},
    {'medium': r'gold\s+leaf', 'qid': 'Q929186', 'paint': False},
    {'medium': r'gold', 'qid': 'Q208045', 'paint': False},
    {'medium': r'gouache', 'qid': 'Q204330', 'paint': True},
    {'medium': r'graphite', 'qid': 'Q5309', 'paint': False},
    {'medium': r'gum\s+arabic', 'qid': 'Q535814', 'paint': False},
    {'medium': r'ivory', 'qid': 'Q82001', 'paint': False},
    {'medium': r'lacquer', 'qid': 'Q11236878', 'paint': False},
    {'medium': r'lead', 'qid': 'Q708', 'paint': False},
    {'medium': r'letterpress', 'qid': 'Q582102', 'paint': False},
    {'medium': r'lithograph', 'qid': 'Q15123870', 'paint': False},
    {'medium': r'marble', 'qid': 'Q40861', 'paint': False},
    {'medium': r'mezzotint', 'qid': 'Q731980', 'paint': False},
    {'medium': r'mixed\s+media', 'qid': 'Q1902763', 'paint': False},
    {'medium': r'monotype', 'qid': 'Q22669635', 'paint': False},
    {'medium': r'oil', 'qid': 'Q296955', 'paint': True},
    {'medium': r'papier\s+mache', 'qid': 'Q899363', 'paint': False},
    {'medium': r'papier\s+mâché', 'qid': 'Q899363', 'paint': False},
    {'medium': r'pastel', 'qid': 'Q189085', 'paint': False},
    {'medium': r'pearl\s*ware', 'qid': 'Q98807132', 'paint': False},
    {'medium': r'photogravure', 'qid': 'Q23657361', 'paint': False},
    {'medium': r'plaster', 'qid': 'Q274988', 'paint': False},
    {'medium': r'porcelain', 'qid': 'Q130693', 'paint': False},
    {'medium': r'portland\s+(|lime)stone', 'qid': 'Q82337', 'paint': False},
    {'medium': r'silkscreen', 'qid': 'Q187791', 'paint': False},
    {'medium': r'screen\s?print', 'qid': 'Q22569957', 'paint': False},
    {'medium': r'slate', 'qid': 'Q207079', 'paint': False},
    {'medium': r'tempera', 'qid': 'Q175166', 'paint': True},
    {'medium': r'terracotta', 'qid': 'Q60424', 'paint': False},
    {'medium': r'varnish', 'qid': 'Q81683', 'paint': False},
    {'medium': r'vinyl', 'qid': 'xxx', 'paint': False},
    {'medium': r'wash', 'qid': 'Q1469362', 'paint': True},
    {'medium': r'watercolou?r', 'qid': 'Q22915256', 'paint': True},
    {'medium': r'wax', 'qid': 'Q69158', 'paint': False},
    {'medium': r'paint', 'qid': 'Q174219', 'paint': True},                     # paint is last
]

'''
Surfaces found in Yale artworks
    - Surface text
    - Wikidata Qid
'''
surfaces = [
    {'surface': r'canvas', 'qid': 'Q12321255'},
    {'surface': r'cardboard', 'qid': 'Q389782'},
    {'surface': r'board', 'qid': 'Q18668582'},
    {'surface': r'card', 'qid': 'Q6432723'},
    {'surface': r'newsprint', 'qid': 'Q187046'},
    {'surface': r'panel', 'qid': 'Q1348059'},
    {'surface': r'photographic\s+paper', 'qid': 'Q912760'},
    {'surface': r'handmade paper', 'qid': 'Q65769963'},
    {'surface': r'laid\s+paper', 'qid': 'Q1513685'},
    {'surface': r'wove\s+paper', 'qid': 'Q21279007'},
    {'surface': r'paper', 'qid': 'Q11472'},
    {'surface': r'parchment', 'qid': 'Q226697'},
    {'surface': r'silk', 'qid': 'Q37681'},
    {'surface': r'steel', 'qid': 'Q11427'},
    {'surface': r'vellum', 'qid': 'Q378274'},
    {'surface': r'vinyl', 'qid': 'Q1812439'},
    {'surface': r'wood', 'qid': 'Q287'},
]


# ----------------------------------------------------------------------

if __name__ == '__main__':
    main()
