#!/usr/bin/env python3.10
# Created by AJIOB, 2022
#
# Full restoring sequence should be:
# 1. Using test repo with the same sources, as in original
# 2. Restoring all PRs & branches (csv with PRs, $5 should be empty, $6 should be with images path)
# 3. Restoring all PR comments (csv with PR comments, $5 should be empty, $6 should be with images path, $7 should be with JSON file)
# 4. Close all PRs (any csv, $5 = '-cPRs')
# 5. Delete all created branches (any csv, $5 = '-dBranches')
#
# If results are correct, you can do that on production repo with the same sources, as in test/original repos. Command subsequence will be the equal, as for testing.
#
# Notes:
# - repo name must be the same in restoring time, because of CSV with multiple repo info are supported
# - you should create PR with attached CSV & JSON files for possible next re-restoring in another format or repo
# - tested with Bitbucket Server v8.5.0 (not Bitbucket Cloud)
# - Bitbucket app key (HTTP access token) should be used instead of real password (with repository write permissions)
# - as told in Bitbucket REST API docs (https://docs.atlassian.com/bitbucket-server/rest/5.16.0/bitbucket-rest.html, "Personal Repositories" part), if you want to access user project instead of workspace project, you should add '~' before your username. For example, use '~alex/my-repo' for accessing 'alex' personal workspace
# - all cross-referenced repositories should be in one new workgroup
#
# Args sequence:
## $1 = source file name (csv-formatted data from ruby)
## $2 = server URL (such as 'https://bitbucket.org/')
## $3 = server auth info: "username:password"
## $4 = server project/repo combination (such as 'my-workspace/test-repo')
## $5 = (optional) additional options:
### -uAll = load info from file to PRs with auto-detection PRs/PR comments
### -debug = print input variables & exit
### -uPRs = load info from file to PRs (not recreate branches)
### -uPRsIncremental = load info from file to PRs (not recreate branches, check if PR was already created)
### -dAll = delete all created branches & PRs
### -dBranches = delete all created branches (keep PRs)
### -cPRs = close (decline) all created PRs
### -dPRs = delete all created PRs (keep branches)
## $6 = (optional) source server url. Default value is bitbucket cloud URL
## $7..$x = (optional) additional args
### any_filename.json = json file will additional info:
#### - PR comments uses that info in format key:value, where key = diff URL (usually bitbucket API), value = downloaded diff info from that URL
### any_foldername/ = folder will additional info:
#### - PRs & PR comments uses files as mirrors for images uploading

import aiohttp
import asyncio
import csv
from datetime import datetime
from enum import Enum
import json
import os
import re
import sys
from urllib.parse import unquote
from zoneinfo import ZoneInfo

OPENED_PR_STATE = "OPEN"
ANY_PR_STATE = "ALL"
SRC_BRANCH_PREFIX = 'src'
DST_BRANCH_PREFIX = 'dst'
# URL match regex from https://uibakery.io/regex-library/url-regex-python
URLS_REGEX = r'https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&\/=]*)'
# Used by creation & filtering too, uses '[' for generating more specific output
PR_START_NAME = "[Bitbucket Import"
BRANCH_START_NAME = "bitbucket/"
# May be set to None for enable default limit 25
DEFAULT_PAGE_RECORDS_LIMIT = 2000
# Exit if it was found
HTTP_EXIT_CODES = [
    # HTTP 401 Unauthorized error code
    401,
]
# Limit number of simultaneous requests. With big number we will have lots of miss-generated 500 errors
LIMIT_NUMBER_SIMULTANEOUS_REQUESTS = 25
LIMIT_NUMBER_SIMULTANEOUS_REQUESTS_BRANCH_DELETE = 10
# True => print attached diffs
# False => don't diffs as attaches
PRINT_ATTACHED_DIFFS = False
TARGET_COMMENTS_TIMEZONE = ZoneInfo("Europe/Moscow")
SOURCE_SERVER_ABSOLUTE_URL_PREFIX = ""
SOURCE_SERVER_IMAGES_SUFFIX = ".png"

class ProcessingMode(Enum):
    LOAD_INFO = 1
    DELETE_BRANCHES = 2
    DELETE_BRANCHES_PRS = 3
    DELETE_PRS = 4
    LOAD_INFO_ONLY_PRS = 5
    CLOSE_PRS = 6
    DEBUG = 7
    LOAD_INFO_ONLY_PRS_INCREMENTAL = 8

CURRENT_MODE = None
JSON_ADDITIONAL_INFO = {}
IMAGES_ADDITIONAL_INFO_PATH = ''

class PullRequest:
    def __init__(self, id, user, title, state, createdAt, closedAt, body, bodyHtml, srcCommit, dstCommit, srcBranch, dstBranch, declineReason, mergeCommit, closedBy):
        self.id = id
        self.user = user
        self.title = title
        self.state = state
        self.createdAt = createdAt
        self.closedAt = closedAt
        self.body = body
        self.bodyHtml = bodyHtml
        self.srcCommit = srcCommit
        self.dstCommit = dstCommit
        self.srcBranch = srcBranch
        self.dstBranch = dstBranch
        self.declineReason = declineReason
        self.mergeCommit = mergeCommit
        self.closedBy = closedBy

class PRComment:
    def __init__(self, repo, prNumber, user, currType, currId, body, bodyHtml, createdAt, isDeleted, toLine, fromLine, file, diffUrl, parentComment, commit):
        self.repo = repo
        self.prId = prNumber
        self.user = user
        self.currType = currType
        self.id = currId
        self.body = body
        self.bodyHtml = bodyHtml
        self.createdAt = createdAt
        self.isDeleted = isDeleted
        self.toLine = toLine
        self.fromLine = fromLine
        self.file = file
        self.diffUrl = diffUrl
        self.parentCommentId = parentComment
        self.commit = commit

class PullRequestShort:
    def __init__(self, id, version):
        self.id = id
        self.version = version

def init():
    try:
        # see https://stackoverflow.com/a/15063941/6818663
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        maxLong = (1 << 31) - 1

        # Looks like Windows uses long instead of long long
        csv.field_size_limit(maxLong)

    # Init global templates
    # Almost all from https://docs.atlassian.com/bitbucket-server/rest/5.16.0/bitbucket-rest.html
    global URL_CREATE_PR
    URL_CREATE_PR = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/pull-requests"

    global URL_CREATE_PR_COMMENT
    URL_CREATE_PR_COMMENT = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/pull-requests/{pullRequestId}/comments"

    global URL_CLOSE_PR
    URL_CLOSE_PR = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/pull-requests/{pullRequestId}/decline"

    global URL_DELETE_PR
    URL_DELETE_PR = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/pull-requests/{pullRequestId}"

    global URL_CREATE_BRANCH
    URL_CREATE_BRANCH = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/branches"

    # From https://docs.atlassian.com/bitbucket-server/rest/5.16.0/bitbucket-branch-rest.html
    global URL_DELETE_BRANCH
    URL_DELETE_BRANCH = "{endpoint}rest/branch-utils/{version}/projects/{projectKey}/repos/{repositorySlug}/branches"

    global URL_GET_COMMIT
    URL_GET_COMMIT = "{endpoint}rest/api/{version}/projects/{projectKey}/repos/{repositorySlug}/commits/{commitId}"

    # Hand revert from UI attaching
    global URL_ATTACH_FILE
    URL_ATTACH_FILE = "{endpoint}projects/{projectKey}/repos/{repositorySlug}/attachments"

    # From https://confluence.atlassian.com/cloudkb/xsrf-check-failed-when-calling-cloud-apis-826874382.html
    global POST_HEADERS
    POST_HEADERS = {"X-Atlassian-Token": "no-check"}

    global MULTITHREAD_LIMIT
    MULTITHREAD_LIMIT = asyncio.Semaphore(LIMIT_NUMBER_SIMULTANEOUS_REQUESTS)

    global MULTITHREAD_LIMIT_BRANCH_DELETE
    MULTITHREAD_LIMIT_BRANCH_DELETE = asyncio.Semaphore(LIMIT_NUMBER_SIMULTANEOUS_REQUESTS_BRANCH_DELETE)

def args_read(argv):
    global SRC_FILE
    SRC_FILE = argv[1]

    global SERVER
    SERVER = argv[2]
    if not SERVER.endswith('/'):
        SERVER += '/'

    global SERVER_API_VERSION
    # 1 for custom bitbucket server/datacenter, 2 for cloud
    if 'bitbucket.org' in SERVER:
        SERVER_API_VERSION = 2
    else:
        SERVER_API_VERSION = 1

    SERVER_API_VERSION = f"{SERVER_API_VERSION}.0"

    USER_PASS = argv[3]
    userPassSplit = USER_PASS.split(':')

    global AUTH
    AUTH = aiohttp.BasicAuth(userPassSplit[0], userPassSplit[1])

    PROJECT_REPO = argv[4]
    prjRepoSplit = PROJECT_REPO.split('/')

    global PROJECT
    global REPO

    PROJECT = prjRepoSplit[0].lower()
    REPO = prjRepoSplit[1].lower()

    if len(argv) > 5:
        global CURRENT_MODE
        mode = argv[5]
        if mode == '-dAll':
            CURRENT_MODE = ProcessingMode.DELETE_BRANCHES_PRS
        elif mode == '-uAll':
            CURRENT_MODE = ProcessingMode.LOAD_INFO
        elif mode == '-dBranches':
            CURRENT_MODE = ProcessingMode.DELETE_BRANCHES
        elif mode == '-dPRs':
            CURRENT_MODE = ProcessingMode.DELETE_PRS
        elif mode == '-uPRs':
            CURRENT_MODE = ProcessingMode.LOAD_INFO_ONLY_PRS
        elif mode == '-uPRsIncremental':
            CURRENT_MODE = ProcessingMode.LOAD_INFO_ONLY_PRS_INCREMENTAL
        elif mode == '-cPRs':
            CURRENT_MODE = ProcessingMode.CLOSE_PRS
        elif mode == '-debug':
            CURRENT_MODE = ProcessingMode.DEBUG
        else:
            CURRENT_MODE = None

    global SOURCE_SERVER_ABSOLUTE_URL_PREFIX
    SOURCE_SERVER_ABSOLUTE_URL_PREFIX = "https://bitbucket.org/"
    if len(argv) > 6:
        if re.fullmatch(URLS_REGEX, argv[6]):
            SOURCE_SERVER_ABSOLUTE_URL_PREFIX = argv[6]
            if not SOURCE_SERVER_ABSOLUTE_URL_PREFIX.endswith('/'):
                SOURCE_SERVER_ABSOLUTE_URL_PREFIX += '/'

    global JSON_ADDITIONAL_INFO
    JSON_ADDITIONAL_INFO = {}
    global IMAGES_ADDITIONAL_INFO_PATH
    IMAGES_ADDITIONAL_INFO_PATH = ''
    for p in argv[7:]:
        if p.endswith('.json'):
            with open(p, "r", encoding="utf8") as f:
                JSON_ADDITIONAL_INFO = json.load(f)
        elif p.endswith('/'):
            IMAGES_ADDITIONAL_INFO_PATH = p

def read_file(path):
    rows = []
    with open(path, "r", encoding="utf8") as src:
        inReader = csv.reader(src)

        for row in inReader:
            rows.append(row)

    return rows

def formatTemplate(template, prId=None, commitId=None):
    return template.format(
        endpoint=SERVER,
        version=SERVER_API_VERSION,
        projectKey=PROJECT,
        repositorySlug=REPO,
        pullRequestId=prId,
        commitId=commitId
    )

def formatBranchName(id, prefix, originalName):
    res = f'{BRANCH_START_NAME}{id}/{prefix}/{originalName}'

    # we have limit of 111 chars
    # but it looks like need to be limited by 100:
    # https://jira.atlassian.com/browse/BSERV-10433
    return res[:100]

def append_timestamp_string_if_possible(text, textUtcSeconds, targetTimeZone=TARGET_COMMENTS_TIMEZONE, errorDescription=None):
    try:
        dt = datetime.utcfromtimestamp(int(textUtcSeconds))
    except Exception as e:
        if errorDescription:
            print(errorDescription)
        print(e)
        print()

        return text

    try:
        dt = dt.replace(tzinfo=targetTimeZone)
    except Exception as e:
        print("Bad user-provided time-zone")
        print(e)
        print()

        return text

    return f"{text} at {dt.isoformat()}"

async def response_process(res):
    # Force waiting message, as described here:
    # https://stackoverflow.com/a/56446507/6818663
    text = await res.text()

    try:
        res.raise_for_status()
        pass
    except aiohttp.ClientResponseError as e:
        # Force fill message field & rethrow exception
        raise aiohttp.ClientResponseError(e.request_info, e.history, status=e.status, headers=e.headers, message=text)

    return text

async def create_pr(session, title, description = None, srcBranch = "prTest1", dstBranch = "stage"):
    payload = {
        "title": title,
        "description": description,
        "fromRef": {
            "id": srcBranch,
        },
        "toRef": {
            "id": dstBranch,
        },
        "reviewers": [
        ]
    }

    async with MULTITHREAD_LIMIT:
        async with session.post(formatTemplate(URL_CREATE_PR), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def list_prs(session, start=0, state=OPENED_PR_STATE, filterText=None):
    payload = {
        "start": start,
        "state": state,
    }

    if DEFAULT_PAGE_RECORDS_LIMIT:
        payload["limit"] = DEFAULT_PAGE_RECORDS_LIMIT

    if filterText:
        payload["filterText"] = filterText

    async with MULTITHREAD_LIMIT:
        async with session.get(formatTemplate(URL_CREATE_PR), auth=AUTH, params=payload) as resp:
            return await response_process(resp)

async def close_pr(session, id, version, comment=None):
    payload = {
        "version": version,
    }

    if comment:
        payload["comment"] = comment

    async with MULTITHREAD_LIMIT:
        async with session.post(formatTemplate(URL_CLOSE_PR, prId=id), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def delete_pr(session, id, version):
    payload = {
        "version": version,
    }

    async with MULTITHREAD_LIMIT:
        async with session.delete(formatTemplate(URL_DELETE_PR, prId=id), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def create_pr_file_comment(session, prId, text, filePath, lineNum, fileType="TO", lineType="CONTEXT", fromHash=None, toHash=None, diffType="RANGE"):
    payload = {
        "text": text,
        "anchor": {
            "line": lineNum,
            "lineType": lineType,
            "fileType": fileType,
            "path": filePath,
        },
    }

    if fromHash or toHash:
        payload["anchor"]["fromHash"] = fromHash
        payload["anchor"]["toHash"] = toHash
        payload["anchor"]["diffType"] = diffType

    async with MULTITHREAD_LIMIT:
        async with session.post(formatTemplate(URL_CREATE_PR_COMMENT, prId=prId), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def create_pr_comment(session, prId, text, parentCommit=None):
    payload = {
        "text": text,
    }

    if parentCommit:
        payload["parent"] = {
            "id": parentCommit,
        }

    async with MULTITHREAD_LIMIT:
        async with session.post(formatTemplate(URL_CREATE_PR_COMMENT, prId=prId), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def get_commit_info(session, commitToRead):
    async with MULTITHREAD_LIMIT:
        async with session.get(formatTemplate(URL_GET_COMMIT, commitId = commitToRead), auth=AUTH) as resp:
            return await response_process(resp)

async def attach_file(session, filePathToAttach):
    fileName = os.path.basename(filePathToAttach)

    with open(filePathToAttach, "rb") as f:
        with aiohttp.MultipartWriter('form-data') as mpwriter:
            part = mpwriter.append(f)
            part.set_content_disposition('form-data', name='files', filename=fileName)

            async with MULTITHREAD_LIMIT:
                async with session.post(formatTemplate(URL_ATTACH_FILE), auth=AUTH, data=mpwriter) as resp:
                    return await response_process(resp)

async def create_branch(session, name, commit):
    payload = {
        "name": name,
        "startPoint": commit,
    }

    async with MULTITHREAD_LIMIT:
        async with session.post(formatTemplate(URL_CREATE_BRANCH), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
            return await response_process(resp)

async def list_branches(session, filterText=None, start=0):
    payload = {
        "start": start,
    }

    if filterText != None:
        payload["filterText"] = filterText

    if DEFAULT_PAGE_RECORDS_LIMIT:
        payload["limit"] = DEFAULT_PAGE_RECORDS_LIMIT

    async with MULTITHREAD_LIMIT:
        async with session.get(formatTemplate(URL_CREATE_BRANCH), auth=AUTH, params=payload) as resp:
            return await response_process(resp)

async def delete_branch(session, id, dryRun=False):
    payload = {
        "name": id,
        "dryRun": dryRun,
    }

    # Force blocking multithread deleting, because git almost not support concurrency on bitbucket backend
    async with MULTITHREAD_LIMIT_BRANCH_DELETE:
        async with MULTITHREAD_LIMIT:
            async with session.delete(formatTemplate(URL_DELETE_BRANCH, prId=id), auth=AUTH, headers=POST_HEADERS, json=payload) as resp:
                return await response_process(resp)

async def upload_prs(session, data):
    headers = data[0]

    prs = []

    # Parse data
    for d in data[1:]:
        repo = d[headers.index('Repository')]
        number = d[headers.index('#')]
        user = d[headers.index('User')]
        title = d[headers.index('Title')]
        state = d[headers.index('State')]
        createdAt = d[headers.index('CreatedAt')]
        closedAt = d[headers.index('UpdatedAt')]
        body = d[headers.index('BodyRaw')]
        bodyHtml = d[headers.index('BodyHTML')]
        src = d[headers.index('SourceCommit')]
        dst = d[headers.index('DestinationCommit')]
        srcBranch = d[headers.index('SourceBranch')]
        dstBranch = d[headers.index('DestinationBranch')]
        declineReason = d[headers.index('DeclineReason')]
        mergeCommit = d[headers.index('MergeCommit')]
        closedBy = d[headers.index('ClosedBy')]

        if repo.lower() != REPO:
            # Block creating another PRs
            continue

        pr = PullRequest(number, user, title, state, createdAt, closedAt, body, bodyHtml, src, dst, srcBranch, dstBranch, declineReason, mergeCommit, closedBy)
        prs.append(pr)

    # Should create old PRs at the beginning
    prs.reverse()

    if CURRENT_MODE == ProcessingMode.LOAD_INFO:
        # Create branches
        await asyncio.gather(*[create_branches_for_pr(session, pr) for pr in prs])

    # Create pull requests
    res = await asyncio.gather(*[upload_single_pr(session, pr) for pr in prs])
    return sum(res)

async def create_branches_for_pr(session, pr):
    try:
        print("Creating branches for PR", pr.id)

        srcCommit = pr.srcCommit
        try:
            res = await get_commit_info(session, srcCommit)

            # force rechecking for empty commits
            res = json.loads(res)
            commitId = res["id"]
        except Exception as e:
            # Commit not found, using merge commit
            srcCommit = pr.mergeCommit

            print("Using merge commit instead of src (second not presented in subtree), PR", pr.id)

        # If merge commit is not presented, we will see that next in create part

        await create_branch(session, formatBranchName(pr.id, SRC_BRANCH_PREFIX, pr.srcBranch), srcCommit)
        await create_branch(session, formatBranchName(pr.id, DST_BRANCH_PREFIX, pr.dstBranch), pr.dstCommit)
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught for PR {pr.id} branch creation")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught for PR {pr.id} branch creation")
        print(e)
        print()

async def upload_single_pr(session, pr):
    try:
        # First number in title must be original PR number
        # for correct comments uploading
        newTitle = f"{PR_START_NAME} {pr.id}, {pr.state}] {pr.title}"

        descriptionParts = [
            f"",
            f"Source commit (from) {pr.srcCommit} (branch ***{pr.srcBranch}***)",
            f"Destination commit (to) {pr.dstCommit} (branch ***{pr.dstBranch}***)",
            f"",
        ]

        if pr.state != OPENED_PR_STATE:
            desc = f"_Closed by **{pr.closedBy}**"
            closeInfo = append_timestamp_string_if_possible(desc, pr.closedAt, errorDescription=f"Bad PR {pr.id} closing date:") + "_"

            descriptionParts.insert(0, closeInfo)

        desc = f"_Created by **{pr.user}**"
        createInfo = append_timestamp_string_if_possible(desc, pr.createdAt, errorDescription=f"Bad PR {pr.id} creation date:") + "_"
        descriptionParts.insert(0, createInfo)

        if pr.declineReason != '':
            descriptionParts.append("Decline message:")
            descriptionParts.append(pr.declineReason)
            descriptionParts.append('')

        if pr.mergeCommit != '':
            descriptionParts.append(f"Merged to commit {pr.mergeCommit}")
            descriptionParts.append('')

        descriptionParts.append("Original description:")
        descriptionParts.append(await pr_all_process_body(session, pr))

        newDescription = '\n'.join(descriptionParts)

        print("Creating PR", pr.id)

        await create_pr(session, newTitle, newDescription, formatBranchName(pr.id, SRC_BRANCH_PREFIX, pr.srcBranch), formatBranchName(pr.id, DST_BRANCH_PREFIX, pr.dstBranch))

        return 0
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught for PR {pr.id} PR creation")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught for PR {pr.id} PR creation")
        print(e)
        print()

    # PR was not created
    return 1

async def pr_all_process_body(session, prOrComment):
    raw = prOrComment.body
    html = prOrComment.bodyHtml

    # Processing username cites
    # They are formatted in raw body as @{GUID} or @{number:GUID}
    matches = re.findall(r'@\{(?:\d+:)?[0-9A-Fa-f-]+\}', raw)
    # Force making matches unique
    matches = set(matches)
    ## need to replace all matches
    for m in matches:
        realId = m[2:-1]
        # '@' not in match for removing unwanted triggers
        idSearch = re.search(re.escape(realId) + r'"[^>]*>@([^<>]*)</', html)
        if not idSearch:
            print(f"Corrupted HTML message for object {prOrComment.id}")
            continue

        realUser = idSearch.group(1)

        # User name will be bold & italic
        raw = raw.replace(m, f"***{realUser}***")

    # Processing absolute URLs for bitbucket
    matches = re.findall(URLS_REGEX, raw)
    for i, m in enumerate(matches):
        if m.endswith(')'):
            # That is regex bug
            matches[i] = m[:-1]

    matches = set(matches)
    for m in matches:
        if not m.startswith(SOURCE_SERVER_ABSOLUTE_URL_PREFIX):
            print(f"Unsupported URL '{m}' was detected for for PR/PR comment {prOrComment.id}. Skipping it")
            continue

        if m.endswith(SOURCE_SERVER_IMAGES_SUFFIX):
            # This is image, need to check

            # Based on name encoding on saving
            diskFileName = unquote(m).replace(':', '_').replace('/', '_')
            # IMAGES_ADDITIONAL_INFO_PATH already has last '/'
            fullDiskName = IMAGES_ADDITIONAL_INFO_PATH + diskFileName

            try:
                attachRes = await attach_file(session, fullDiskName)
                attachRes = json.loads(attachRes)

                newUrl = attachRes["attachments"][0]["links"]["attachment"]["href"]

                raw = raw.replace(m, newUrl)
            except aiohttp.ClientResponseError as e:
                print(f"Cannot attach file from old URL '{m}'. HTTP error {e.status}, message {e.message}")
            except Exception as e:
                print(f"Cannot attach file from old URL '{m}'. Error message {e}")
        else:
            prIdMatch = re.search(re.escape(SOURCE_SERVER_ABSOLUTE_URL_PREFIX) + r'.*/pull-requests/(\d+)', m)
            if prIdMatch:
                # This is PR or PR comment, should process as PR link
                # TODO: implement
                pass
            else:
                print(f"Unknown URL type '{m}' was detected for PR/PR comment {prOrComment.id}. Skipping it too")


    return raw

# Returns True if base PR comment exists, else False
async def form_single_pr_comment(session, currComment, newCommentIds, prInfo, diffs={}):
    # Receiving PR info
    if not currComment.prId in prInfo:
        print("Old PR", currComment.prId, "was not created. Comment", currComment.id, "cannot be created too")
        return True
    newPr = prInfo[currComment.prId]

    parent = None
    if currComment.parentCommentId:
        if not currComment.parentCommentId in newCommentIds:
            return False

        parent = newCommentIds[currComment.parentCommentId]

    textParts = [
        append_timestamp_string_if_possible(f"_Created by **{currComment.user}** for commit {currComment.commit}", currComment.createdAt, errorDescription=f"Bad PR {currComment.prId} comment {currComment.id} creation date:") + '_',
    ]

    if currComment.isDeleted == 'true':
        print(f"Comment {currComment.id} for original PR {currComment.prId} was deleted")
        textParts.append("Message was previously deleted")
    textParts.append("")

    # Printing before diff, because diff may be very long
    textParts.append(f"Original message:")
    textParts.append(await pr_all_process_body(session, currComment))
    textParts.append("")

    lineNum = None
    if currComment.file != '':
        if currComment.fromLine != '':
            fileType = 'FROM'
            fileTypeText = 'Source'
            lineType = 'REMOVED'
            lineNum = currComment.fromLine
        else:
            fileType = 'TO'
            fileTypeText = 'Current'
            lineType = 'ADDED'
            lineNum = currComment.toLine

        # Printing source file & diff info only for root comment
        if not parent:
            # Force delim info from source message
            textParts.append("----")
            textParts.append(f"Source file ***{currComment.file}***")
            textParts.append(f"{fileTypeText} commit line {lineNum}")
            textParts.append("")

        if currComment.diffUrl:
            if not parent:
                if currComment.diffUrl in diffs:
                    if PRINT_ATTACHED_DIFFS:
                        textParts.append("Original diff:")
                        textParts.append("```diff")
                        textParts.append(diffs[currComment.diffUrl])
                        textParts.append("```")
                else:
                    textParts.append(f"This comment is for an outdated diff")

    # merge parts to single text
    text = '\n'.join(textParts)

    try:
        print("Uploading comment", currComment.id, "for original PR", currComment.prId)

        res = None
        if parent != None:
            res = await create_pr_comment(session, newPr.id, text, parent)
        else:
            if currComment.file:
                try:
                    # Trying to create file with bitbucket diff, not our
                    res = await create_pr_file_comment(session, newPr.id, text, currComment.file, lineNum, fileType, lineType)
                except aiohttp.ClientResponseError as e:
                    print(f"Creating file comment {currComment.id} from PR {currComment.prId} as usual file. HTTP error {e.status}, message {e.message}")
                except Exception as e:
                    print(f"Creating file comment {currComment.id} from PR {currComment.prId} as usual file. Error message {e}")

            # file comment was not created or that is usual comment
            if not res:
                res = await create_pr_comment(session, newPr.id, text)

        res = json.loads(res)
        newCommentIds[currComment.id] = res["id"]

    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught for PR {currComment.prId} comment {currComment.id} creation")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught for PR {currComment.prId} comment {currComment.id} creation")
        print(e)
        print()

    return True

async def upload_pr_comments(session, data):
    headers = data[0]

    comments = []

    # Parse data
    for d in data[1:]:
        repo = d[headers.index('Repository')]
        prNumber = d[headers.index('PRNumber')]
        user = d[headers.index('User')]
        currType = d[headers.index('CommentType')]
        currId = d[headers.index('CommentID')]
        body = d[headers.index('BodyRaw')]
        bodyHtml = d[headers.index('BodyHTML')]
        createdAt = d[headers.index('CreatedAt')]
        isDeleted = d[headers.index('IsDeleted')]
        toLine = d[headers.index('ToLine')]
        fromLine = d[headers.index('FromLine')]
        file = d[headers.index('FilePath')]
        diffUrl = d[headers.index('Diff')]
        parentComment = d[headers.index('ParentID')]
        commit = d[headers.index('CommitHash')]

        if repo.lower() != REPO:
            # Block creating another PRs
            continue

        comment = PRComment(repo, prNumber, user, currType, currId, body, bodyHtml, createdAt, isDeleted, toLine, fromLine, file, diffUrl, parentComment, commit)
        comments.append(comment)

    prInfo = {}

    # Loading PR info
    try:
        print(f"Loading PR info")

        res = await list_all_prs(session, filterTitle=PR_START_NAME)

        for v in res:
            prId = v["id"]
            prTitle = v["title"]
            prVersion = v["version"]

            # get first number, as described in PR title creation
            numberSearch = re.search(r'\d+', prTitle)
            if not numberSearch:
                print(f"Bad PR {prId}: unsupported title: '{prTitle}'")
                continue

            originalPrId = numberSearch.group()

            prInfo[originalPrId] = PullRequestShort(prId, prVersion)
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while loading PR info")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while loading PR info")
        print(e)
        print()

    # key will be old comment id, value will be new comment id
    newCommentIds = {}

    commentsToCheckAgain = comments

    prevCommentNumber = 0

    # Block for infinite loop
    while prevCommentNumber != len(commentsToCheckAgain):
        prevCommentNumber = len(commentsToCheckAgain)
        print(prevCommentNumber, "comments will be checked for loading")

        loadResults = await asyncio.gather(*[form_single_pr_comment(session, c, newCommentIds, prInfo, JSON_ADDITIONAL_INFO) for c in commentsToCheckAgain])

        # check what wasn't uploaded
        newCommentsToCheckAgain = []
        for i in range(prevCommentNumber):
            if not loadResults[i]:
                newCommentsToCheckAgain.append(commentsToCheckAgain[i])

        # save prev iteration as current
        commentsToCheckAgain = newCommentsToCheckAgain

    if len(commentsToCheckAgain) != 0:
        print("Cannot find parents for that comments:")
        for c in commentsToCheckAgain:
            print("ID", c.id, "body", c.body)

async def delete_all_branches(session, filterText=None):
    try:
        while True:
            res = await list_branches(session, filterText)
            res = json.loads(res)

            tasks = []

            for v in res["values"]:
                branchId = v["id"]
                print("Deleting", branchId)
                tasks.append(delete_branch_no_error(session, branchId))

            # Wait all tasks for that iteration
            await asyncio.gather(*tasks)

            if res["isLastPage"]:
                break
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while all branches deleting")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while all branches deleting")
        print(e)
        print()

async def list_all_prs(session, state=ANY_PR_STATE, filterTitle=None):
    allPrs = []

    try:
        start = 0

        while True:
            res = await list_prs(session, start, state, filterTitle)
            res = json.loads(res)

            for v in res["values"]:
                prId = v["id"]
                prTitle = v["title"]
                if filterTitle and not filterTitle in prTitle:
                    start += 1

                    print("Skipping PR", prId, "with title", prTitle)

                    continue

                allPrs.append(v)

            if res["isLastPage"]:
                break

            start = res["nextPageStart"]
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while all PRs closing")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while all PRs closing")
        print(e)
        print()

    return allPrs

async def close_all_prs(session, filterTitle=None):
    state=OPENED_PR_STATE

    try:
        start = 0

        while True:
            res = await list_prs(session, start, state, filterTitle)
            res = json.loads(res)

            tasks = []

            for v in res["values"]:
                prId = v["id"]
                prTitle = v["title"]
                prVersion = v["version"]
                if filterTitle and not filterTitle in prTitle:
                    start += 1

                    print("Skipping PR", prId, "with title", prTitle)

                    continue

                print("Closing PR", prId, "version", prVersion, "with title", prTitle)
                tasks.append(close_pr_no_error(session, prId, prVersion, "Imported pull request"))

            # Wait all tasks for that iteration
            await asyncio.gather(*tasks)

            if res["isLastPage"]:
                break
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while all PRs closing")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while all PRs closing")
        print(e)
        print()

async def delete_all_prs(session, filterTitle=None, state=OPENED_PR_STATE):
    try:
        start = 0

        while True:
            res = await list_prs(session, start, state, filterTitle)
            res = json.loads(res)

            tasks = []

            for v in res["values"]:
                prId = v["id"]
                prTitle = v["title"]
                prVersion = v["version"]
                if filterTitle and not filterTitle in prTitle:
                    start += 1

                    print("Skipping PR", prId, "with title", prTitle)

                    continue

                print("Deleting PR", prId, "with title", prTitle)
                tasks.append(delete_pr_no_error(session, prId, prVersion))

            # Wait all tasks for that iteration
            await asyncio.gather(*tasks)

            if res["isLastPage"]:
                break
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while all PRs deleting")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while all PRs deleting")
        print(e)
        print()

async def delete_branch_no_error(session, branchId):
    try:
        return await delete_branch(session, branchId)
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while deleting branch {branchId}")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while deleting branch {branchId}")
        print(e)
        print()

async def close_pr_no_error(session, id, version, comment):
    try:
        return await close_pr(session, id, version, comment)
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while PR {id} closing")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while PR {id} closing")
        print(e)
        print()

async def delete_pr_no_error(session, prId, prVersion):
    try:
        return await delete_pr(session, prId, prVersion)
    except aiohttp.ClientResponseError as e:
        print(f"HTTP Exception was caught while PR {prId} deleting")
        print(f"HTTP code {e.status}")
        print(e.message)
        print()
        if e.status in HTTP_EXIT_CODES:
            exit(e.status)
    except Exception as e:
        print(f"Exception was caught while PR {prId} deleting")
        print(e)
        print()

async def main(argv):
    init()
    args_read(argv)

    if CURRENT_MODE == None:
        print("Current mode was not selected")
        return

    async with aiohttp.ClientSession() as session:
        await main_select_mode(session)

async def main_select_mode(session):
    if CURRENT_MODE == ProcessingMode.DEBUG:
        print("Src:", SRC_FILE)
        print("URL:", SERVER)
        print("API:", SERVER_API_VERSION)
        print("Auth:", AUTH.login, AUTH.password)
        print("Prj:", PROJECT)
        print("Repo:", REPO)
        print("Source server:", SOURCE_SERVER_ABSOLUTE_URL_PREFIX)
        print("Images folder:", IMAGES_ADDITIONAL_INFO_PATH)
        print("JSON data:", JSON_ADDITIONAL_INFO)

        return

    if CURRENT_MODE == ProcessingMode.CLOSE_PRS:
        await close_all_prs(session, PR_START_NAME)
        return

    if CURRENT_MODE == ProcessingMode.DELETE_BRANCHES_PRS or CURRENT_MODE == ProcessingMode.DELETE_PRS:
        # Must be done before branches removing
        await delete_all_prs(session, PR_START_NAME, ANY_PR_STATE)
    if CURRENT_MODE == ProcessingMode.DELETE_BRANCHES or CURRENT_MODE == ProcessingMode.DELETE_BRANCHES_PRS:
        await delete_all_branches(session, BRANCH_START_NAME)

    if CURRENT_MODE == ProcessingMode.DELETE_BRANCHES or CURRENT_MODE == ProcessingMode.DELETE_BRANCHES_PRS or CURRENT_MODE == ProcessingMode.DELETE_PRS:
        return

    data = read_file(SRC_FILE)
    if len(data) == 0:
        print("Data was empty")
    elif len(data[0]) == 0:
        print("Data header was empty")
    elif data[0][-1] == 'ClosedBy':
        print("PRs were found. Uploading them")
        return await upload_prs(session, data)
    elif data[0][-1] == 'CommitHash':
        print("PRs comments were found. Uploading them")
        await upload_pr_comments(session, data)
    else:
        print("Unknown source file format")

if __name__ == '__main__':
    # force set event loop for execution on python 3.10+
    # Based on https://stackoverflow.com/a/73367187/6818663
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        asyncio.run(main(sys.argv))
    except KeyboardInterrupt:
        pass
