# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import enum
import functools
import json
from collections import OrderedDict
from urllib.parse import urlencode
from urllib.parse import urlparse

import hglib
import requests

from cli_common import log

HGMO_JSON_REV_URL_TEMPLATE = 'https://hg.mozilla.org/mozilla-central/json-rev/{}'
MOZILLA_PHABRICATOR_PROD = 'https://phabricator.services.mozilla.com/api/'

logger = log.get_logger(__name__)


class BuildState(enum.Enum):
    Work = 'work'
    Pass = 'pass'
    Fail = 'fail'


@functools.lru_cache(maxsize=2048)
def revision_exists_on_central(revision):
    url = HGMO_JSON_REV_URL_TEMPLATE.format(revision)
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.ok


def revision_available(repo, revision):
    '''
    Check if a revision is available on a Mercurial repo
    '''
    try:
        repo.identify(revision)
        return True
    except hglib.error.CommandError:
        return False


# Descriptions of the fields are available at
# https://phabricator.services.mozilla.com/conduit/method/harbormaster.sendmessage/,
# in the "Lint Results" paragraph.
class LintResult(dict):
    def __init__(self, name, code, severity, path, line=None, char=None, description=None):
        self['name'] = name
        self['code'] = code
        self['severity'] = severity
        self['path'] = path
        if line is not None:
            self['line'] = line
        if char is not None:
            self['char'] = char
        if description is not None:
            self['description'] = description
        self.validates()

    def validates(self):
        '''
        Check the input is a lint issue compatible with
        '''

        # Check required strings
        for key in ('name', 'code', 'severity', 'path'):
            assert isinstance(self[key], str), '{} should be a string'.format(key)

        # Check the severity is a valid value
        assert self['severity'] in ('advice', 'autofix', 'warning', 'error', 'disabled'), \
            'Invalid severity value: {}'.format(self['severity'])

        # Check optional integers
        for key in ('line', 'char'):
            value = self.get(key)
            if value:
                assert isinstance(value, int), '{} should be an int'.format(key)

        return True


class PhabricatorRevisionNotFoundException(Exception):
    pass


class ConduitError(Exception):
    '''
    Exception to be raised when Phabricator returns an error response.
    '''
    def __init__(self, msg, error_code=None, error_info=None):
        super(ConduitError, self).__init__(msg)
        self.error_code = error_code
        self.error_info = error_info
        logger.warn('Conduit API error {} : {}'.format(
            self.error_code,
            self.error_info or 'unknown'
        ))

    @classmethod
    def raise_if_error(cls, response_body):
        '''
        Raise a ConduitError if the provided response_body was an error.
        '''
        if response_body['error_code'] is not None:
            raise cls(
                response_body.get('error_info'),
                error_code=response_body.get('error_code'),
                error_info=response_body.get('error_info')
            )


class PhabricatorAPI(object):
    '''
    Phabricator Rest API client
    '''
    def __init__(self, api_key, url=MOZILLA_PHABRICATOR_PROD):
        self.api_key = api_key
        self.url = url
        assert self.url.endswith('/api/'), \
            'Phabricator API must end with /api/'

        # Test authentication
        self.user = self.request('user.whoami')
        logger.info('Authenticated on {} as {}'.format(self.url, self.user['realName']))

    @property
    def hostname(self):
        parts = urlparse(self.url)
        return parts.netloc

    def search_diffs(self, diff_phid=None, revision_phid=None, output_cursor=False, **params):
        '''
        Find details of differential diffs from a Differential diff or revision
        Multiple diffs can be returned (when using revision_phid)
        '''
        constraints = {}
        if diff_phid is not None:
            if isinstance(diff_phid, str):
                constraints['phids'] = [diff_phid, ]
            elif isinstance(diff_phid, list):
                constraints['phids'] = diff_phid
            else:
                raise Exception('diff_phid must be a string or a list')
        if revision_phid is not None:
            constraints['revisionPHIDs'] = [revision_phid, ]
        out = self.request('differential.diff.search', constraints=constraints, **params)

        def _clean(diff):

            # Make all fields easily accessible
            if 'fields' in diff and isinstance(diff['fields'], dict):
                diff.update(diff['fields'])
                del diff['fields']

            # Lookup base revision in refs
            diff['refs'] = {
                ref['type']: ref
                for ref in diff['refs']
            }
            try:
                diff['baseRevision'] = diff['refs']['base']['identifier']
            except KeyError:
                diff['baseRevision'] = None

            return diff

        diffs = list(map(_clean, out['data']))
        if output_cursor is True:
            return diffs, out['cursor']
        return diffs

    def load_raw_diff(self, diff_id):
        '''
        Load the raw diff content
        '''
        return self.request(
            'differential.getrawdiff',
            diffID=diff_id,
        )

    def load_revision(self, rev_phid=None, rev_id=None):
        '''
        Find details of a differential revision
        '''
        assert (rev_phid is not None) ^ (rev_id is not None), 'One and only one of rev_phid or rev_id should be passed'

        constraints = {}
        if rev_id is not None:
            constraints['ids'] = [rev_id, ]
        if rev_phid is not None:
            constraints['phids'] = [rev_phid, ]

        out = self.request(
            'differential.revision.search',
            constraints=constraints,
        )

        data = out['data']
        if len(data) != 1:
            raise PhabricatorRevisionNotFoundException()
        return data[0]

    def list_repositories(self):
        '''
        List available repositories
        '''
        out = self.request('diffusion.repository.search')
        return out['data']

    def list_comments(self, revision_phid):
        '''
        List and format existing inline comments for a revision
        '''
        transactions = self.request(
            'transaction.search',
            objectIdentifier=revision_phid,
        )
        return [
            {
                'diffID': transaction['fields']['diff']['id'],
                'filePath': transaction['fields']['path'],
                'lineNumber': transaction['fields']['line'],
                'lineLength': transaction['fields']['length'],
                'content': comment['content']['raw'],
            }
            for transaction in transactions['data']
            for comment in transaction['comments']
            if transaction['type'] == 'inline' and transaction['authorPHID'] == self.user['phid']
        ]

    def comment(self, revision_id, message):
        '''
        Comment on a Differential revision
        Using a frozen method as new transactions does not
        seem to support inlines publication
        '''
        return self.request(
            'differential.createcomment',
            revision_id=revision_id,
            message=message,
            attach_inlines=1,
        )

    def load_parents(self, revision_phid):
        '''
        Recursively load parents from a stack of revision
        '''
        parents, phids = [], [revision_phid, ]
        while phids:
            phid = phids.pop()
            out = self.request(
                'edge.search',
                types=['revision.parent', ],
                sourcePHIDs=[phid, ]
            )
            for element in out['data']:
                rev = element['destinationPHID']
                if rev in parents:
                    break

                parents.append(rev)
                phids.append(rev)

        return parents

    def load_or_create_build_autotarget(self, object_phid, target_keys):
        '''
        Retrieve or create a build autotarget.
        '''
        res = self.request(
            'harbormaster.queryautotargets',
            objectPHID=object_phid,
            targetKeys=target_keys
        )
        return res['targetMap']

    def search_buildable(self, object_phid):
        '''
        Search HarborMaster buildables linked to an object (diff, revision, ...)
        '''
        constraints = {
            'objectPHIDs': [object_phid, ],
        }
        out = self.request(
            'harbormaster.buildable.search',
            constraints=constraints,
        )
        return out['data']

    def search_build(self, buildable_phid, plans=[]):
        '''
        Search HarborMaster build for a buildable
        Supports HarborMaster Build Plan filtering
        '''
        constraints = {
            'buildables': [buildable_phid, ],
            'plans': plans,
        }
        out = self.request(
            'harbormaster.build.search',
            constraints=constraints,
        )
        return out['data']

    def search_build_target(self, build_phid):
        '''
        Search HarborMaster build targets for a build
        '''
        constraints = {
            'buildPHIDs': [build_phid, ],
        }
        out = self.request(
            'harbormaster.target.search',
            constraints=constraints,
        )
        return out['data']

    def find_diff_build(self, object_phid, build_plan_phid):
        '''
        Find a specific build and its targets for a Diff and an HarborMaster build plan
        '''
        assert isinstance(object_phid, str)
        assert object_phid[0:10] in ('PHID-DIFF-', 'PHID-DREV-')
        assert build_plan_phid.startswith('PHID-HMCP-')

        # First find the buildable for this diff
        buildables = self.search_buildable(object_phid)
        assert len(buildables) == 1
        buildable = buildables[0]
        logger.info('Found HarborMaster buildable', id=buildable['id'], phid=buildable['phid'])

        # Then find the build in that buildable & plan
        builds = self.search_build(buildable['phid'], plans=[build_plan_phid, ])
        assert len(buildables) == 1
        build = builds[0]
        logger.info('Found HarborMaster build', id=build['id'], phid=build['phid'])

        # Finally look for the build targets
        targets = self.search_build_target(build['phid'])
        logger.info('Found HarborMaster build targets', nb=len(targets))

        return build, targets

    def update_build_target(self, build_target_phid, state, unit=[], lint=[]):
        '''
        Update unit test / linting data for a given build target.
        '''
        assert all(map(lambda i: isinstance(i, LintResult), lint)), \
            'Only support LintResult instances'
        assert isinstance(state, BuildState)
        self.request(
            'harbormaster.sendmessage',
            buildTargetPHID=build_target_phid,
            type=state.value,
            unit=unit,
            lint=lint,
        )

    def upload_coverage_results(self, object_phid, coverage_data):
        '''
        Upload code coverage results to a Phabricator object.

        `coverage_data` is an object in the format:
        {
            "this/is/a/path1": "UNCXUNCXUNCX",
            "this/is/a/path2": "UUU",
        }

        The keys of the object are paths to the source files in the mozilla-central
        repository.
        The values are strings defining the coverage for each line of the source file
        (one character per line), where:
        - U means "not covered";
        - N means "not executable";
        - C means "covered";
        - X means that no data is available about that line.
        '''
        # TODO: We are temporarily using arcanist.unit, but we should switch to something
        # different after https://bugzilla.mozilla.org/show_bug.cgi?id=1487843 is resolved.
        res = self.load_or_create_build_autotarget(object_phid, ['arcanist.unit'])
        build_target_phid = res['arcanist.unit']

        self.update_build_target(
            build_target_phid,
            BuildState.Pass,
            unit=[
                {
                    'name': 'Aggregate coverage information',
                    'result': 'pass',
                    'coverage': coverage_data,
                }
            ]
        )

    def upload_lint_results(self, object_phid, state, lint_data):
        '''
        Upload linting/static analysis results to a Phabricator object.

        `type` is either "pass" if no errors were found, "fail" otherwise.

        `lint_data` is an array of LintResult objects.
        '''
        assert isinstance(state, BuildState)

        # TODO: We are temporarily using arcanist.lint, but we should switch to something
        # different after https://bugzilla.mozilla.org/show_bug.cgi?id=1487843 is resolved.
        res = self.load_or_create_build_autotarget(object_phid, ['arcanist.lint'])
        build_target_phid = res['arcanist.lint']

        self.update_build_target(
            build_target_phid,
            state,
            lint=lint_data,
        )

    def request(self, path, **payload):
        '''
        Send a request to Phabricator API
        '''
        # Add api token to payload
        payload['__conduit__'] = {
            'token': self.api_key,
        }

        # Run POST request on api
        response = requests.post(
            self.url + path,
            data=urlencode({
                'params': json.dumps(payload),
                'output': 'json'
            }),
        )

        # Check response
        data = response.json()
        assert response.ok
        assert 'error_code' in data
        ConduitError.raise_if_error(data)

        # Outputs result
        assert 'result' in data
        return data['result']

    def load_patches_stack(self, repo, diff, default_revision='central'):
        '''
        Load full stack of patches from Phabricator into a mercurial repository:
        * uses a diff dict from search_diffs
        * setup repo to base revision from Mozilla Central
        * Apply previous needed patches from Phabricator
        '''
        assert isinstance(repo, hglib.client.hgclient)
        assert isinstance(diff, dict)
        assert 'phid' in diff
        assert 'id' in diff
        assert 'revisionPHID' in diff
        assert 'baseRevision' in diff

        # Diff PHIDs from our patch to its base
        patches = OrderedDict()
        patches[diff['phid']] = diff['id']

        parents = self.load_parents(diff['revisionPHID'])
        if parents:

            # Load all parent diffs
            for parent in parents:
                logger.info('Loading parent diff', phid=parent)

                # Sort parent diffs by their id to load the most recent patch
                parent_diffs = sorted(
                    self.search_diffs(revision_phid=parent),
                    key=lambda x: x['id'],
                )
                last_diff = parent_diffs[-1]
                patches[last_diff['phid']] = last_diff['id']

                # Use base revision of last parent
                hg_base = last_diff['baseRevision']

        else:
            # Use base revision from top diff
            hg_base = diff['baseRevision']

        # When base revision is missing, update to default revision
        if hg_base is None or not revision_available(repo, hg_base):
            logger.warning('Missing base revision from Phabricator', rev=hg_base)
            hg_base = default_revision

        # Load all patches from their numerical ID
        for diff_phid, diff_id in patches.items():
            patches[diff_phid] = self.load_raw_diff(diff_id)

        # Update the repo to base revision
        try:
            logger.info('Updating repo to revision', rev=hg_base)
            repo.update(
                rev=hg_base,
                clean=True,
            )
        except hglib.error.CommandError:
            raise Exception('Failed to update to revision {}'.format(hg_base))

        # Get current revision using full informations tuple from hglib
        revision = repo.identify(id=True).strip()
        revision = repo.log(revision, limit=1)[0]
        logger.info('Updated repo to revision', revision=revision.node)

        # Outputs base revision and patches from the bottom one up to the target
        return (revision, list(reversed(patches.items())))
