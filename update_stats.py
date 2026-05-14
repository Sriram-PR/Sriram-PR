import copy
import datetime
import json
import os
import sys
import time
import hashlib
import tomllib
import requests

try:
    from dateutil import relativedelta
    from lxml import etree
except ImportError as e:
    print(f"Required dependency missing: {e}")
    print("Please install required packages: pip install requests lxml python-dateutil")
    sys.exit(1)

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
if 'ACCESS_TOKEN' not in os.environ or not os.environ['ACCESS_TOKEN']:
    raise ValueError("GitHub ACCESS_TOKEN environment variable must be set")
if 'USER_NAME' not in os.environ or not os.environ['USER_NAME']:
    raise ValueError("USER_NAME environment variable must be set")

# Configuration constants
USER_NAME = os.environ['USER_NAME']
SESSION = requests.Session()
SESSION.headers.update({'authorization': 'token ' + os.environ['ACCESS_TOKEN']})
ENABLE_ARCHIVE = os.environ.get('ENABLE_ARCHIVE', 'true').lower() == 'true'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
               'fetch_repo_loc': 0, 'loc_query': 0}
OWNER_ID = None


class GitHubAPIError(Exception):
    """Non-2xx response or network failure from the GitHub API."""


class RateLimitError(GitHubAPIError):
    """Hit GitHub's documented or undocumented rate limit."""


def daily_readme(birthday):
    """Returns 'XX years, XX months, XX days' since birthday."""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    def plural(n):
        return 's' if n != 1 else ''
    cake = ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    return f"{diff.years} year{plural(diff.years)}, {diff.months} month{plural(diff.months)}, {diff.days} day{plural(diff.days)}{cake}"


def simple_request(func_name, query, variables):
    """Send a GraphQL request. Raises GitHubAPIError on non-200, RateLimitError on 403."""
    query_count(func_name)
    try:
        request = SESSION.post('https://api.github.com/graphql',
                               json={'query': query, 'variables': variables},
                               timeout=30)
    except requests.RequestException as e:
        raise GitHubAPIError(f"{func_name} request failed: {e}") from e

    if request.status_code == 200:
        return request
    if request.status_code == 403:
        raise RateLimitError(f"{func_name} hit rate limit: {request.text}")
    raise GitHubAPIError(f"{func_name} failed with status {request.status_code}: {request.text}")


def graph_repos_stars(count_type, owner_affiliation):
    """Return repo count, star count, or both. count_type: 'repos' | 'stars' | 'both'."""
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''

    cursor = None
    total_stars = 0
    total_count = 0
    while True:
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        repos = simple_request('graph_repos_stars', query, variables).json()['data']['user']['repositories']
        total_count = repos['totalCount']

        if count_type == 'repos':
            return total_count

        total_stars += stars_counter(repos['edges'])
        if not repos['pageInfo']['hasNextPage']:
            break
        cursor = repos['pageInfo']['endCursor']

    if count_type == 'stars':
        return total_stars
    return (total_count, total_stars)


def stars_counter(data):
    """Sum stargazers totalCount across all repository edges."""
    return sum(node['node']['stargazers']['totalCount'] for node in data)


def _fetch_history_page(owner, repo_name, cursor, cache):
    """Fetch one page of commit history with 5xx retry. Returns history dict or None (no default branch)."""
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    max_retries = 5
    for attempt in range(max_retries):
        try:
            query_count('fetch_repo_loc')
            request = SESSION.post('https://api.github.com/graphql',
                                   json={'query': query, 'variables': variables},
                                   timeout=30)
            if request.status_code == 200:
                branch_ref = request.json()['data']['repository']['defaultBranchRef']
                return branch_ref['target']['history'] if branch_ref else None

            if 500 <= request.status_code < 600 and attempt < max_retries - 1:
                backoff = 2 ** attempt
                print(f"  fetch_repo_loc got {request.status_code}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(backoff)
                continue

            force_close_file(cache)
            if request.status_code == 403:
                raise RateLimitError("fetch_repo_loc hit anti-abuse rate limit")
            raise GitHubAPIError(f'fetch_repo_loc failed with status {request.status_code}: {request.text}')
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                backoff = 2 ** attempt
                print(f"  fetch_repo_loc network error, retrying in {backoff}s (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(backoff)
                continue
            force_close_file(cache)
            raise GitHubAPIError(f"fetch_repo_loc network error: {e}") from e
    force_close_file(cache)
    raise GitHubAPIError("fetch_repo_loc: exhausted retries")


def fetch_repo_loc(owner, repo_name, cache):
    """Page through commit history, summing my additions/deletions/commits. Returns (add, del, my_commits)."""
    addition_total = 0
    deletion_total = 0
    my_commits = 0
    cursor = None
    while True:
        history = _fetch_history_page(owner, repo_name, cursor, cache)
        if history is None:
            return 0, 0, 0
        for node in history['edges']:
            if node['node']['author']['user'] == OWNER_ID:
                my_commits += 1
                addition_total += node['node']['additions']
                deletion_total += node['node']['deletions']
        if not history['edges'] or not history['pageInfo']['hasNextPage']:
            return addition_total, deletion_total, my_commits
        cursor = history['pageInfo']['endCursor']


def loc_query(owner_affiliation, force_cache=False):
    """Query repositories and compute LOC stats. Returns [additions, deletions, net, cached_status]."""
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''

    edges = []
    cursor = None
    while True:
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        repo_data = simple_request('loc_query', query, variables).json()['data']['user']['repositories']
        edges += repo_data['edges']
        if not repo_data['pageInfo']['hasNextPage']:
            break
        cursor = repo_data['pageInfo']['endCursor']

    return cache_builder(edges, force_cache)


_EMPTY_ENTRY = {"commits": 0, "my_commits": 0, "loc_add": 0, "loc_del": 0}


def cache_builder(edges, force_cache, loc_add=0, loc_del=0):
    """Builds and maintains the cache of repository data. Returns [additions, deletions, net, cached]."""
    cached = True
    filename = get_cache_filename(USER_NAME)
    cache = _load_cache(filename)

    # Build the set of current repo hashes
    current_hashes = {
        hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        for edge in edges
    }

    if set(cache.keys()) != current_hashes or force_cache:
        # Repo set changed or forced — preserve LOC for matching hashes, drop missing
        cached = False
        cache = {h: cache.get(h, dict(_EMPTY_ENTRY)) for h in current_hashes}

    for edge in edges:
        node = edge['node']
        repo_name = node['nameWithOwner']
        repo_hash = hashlib.sha256(repo_name.encode('utf-8')).hexdigest()
        entry = cache[repo_hash]
        try:
            current_commits = node['defaultBranchRef']['target']['history']['totalCount']
            if entry['commits'] != current_commits:
                owner, name = repo_name.split('/')
                loc = fetch_repo_loc(owner, name, cache)
                cache[repo_hash] = {
                    "commits": current_commits,
                    "my_commits": loc[2],
                    "loc_add": loc[0],
                    "loc_del": loc[1],
                }
        except TypeError:  # repo is empty
            cache[repo_hash] = dict(_EMPTY_ENTRY)

    _save_cache(filename, cache)

    for entry in cache.values():
        loc_add += entry['loc_add']
        loc_del += entry['loc_del']

    return [loc_add, loc_del, loc_add - loc_del, cached]


def get_cache_filename(username):
    """Cache filename for a user (JSON)."""
    os.makedirs('cache', exist_ok=True)
    return f"cache/{hashlib.sha256(username.encode('utf-8')).hexdigest()}.json"


def _load_cache(filename):
    """Load JSON cache, return dict keyed by repo hash. Empty dict if missing or invalid."""
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(filename, cache):
    with open(filename, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def add_archive():
    """Parse cache/repository_archive.txt. Returns [additions, deletions, net, commit_count, repo_count]."""
    try:
        with open('cache/repository_archive.txt', 'r') as f:
            data = f.readlines()
            
        # Remove the comment block at the beginning and end
        content_lines = data[7:len(data)-3]
        
        added_loc, deleted_loc, added_commits = 0, 0, 0
        contributed_repos = len(content_lines)
        
        for line in content_lines:
            repo_hash, total_commits, my_commits, *loc = line.split()
            added_loc += int(loc[0])
            deleted_loc += int(loc[1])
            if my_commits.isdigit():
                added_commits += int(my_commits)
                
        added_commits += int(data[-1].split()[4][:-1])
        return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]
    except FileNotFoundError:
        print("Warning: cache/repository_archive.txt not found. Skipping archive data.")
        return [0, 0, 0, 0, 0]
    except (ValueError, IndexError, KeyError) as e:
        print(f"Error parsing archive data: {e}")
        return [0, 0, 0, 0, 0]
    except OSError as e:
        print(f"Error reading archive file: {e}")
        return [0, 0, 0, 0, 0]


def force_close_file(cache):
    """Save the in-progress cache before raising, so a partial update isn't lost."""
    filename = get_cache_filename(USER_NAME)
    try:
        _save_cache(filename, cache)
        print(f'Saved partial data to {filename} before error.')
    except OSError as e:
        print(f"Error saving cache file: {e}")


def commit_counter():
    """Sum my_commits across all cached repos."""
    cache = _load_cache(get_cache_filename(USER_NAME))
    return sum(entry.get('my_commits', 0) for entry in cache.values())


def user_getter(username):
    """Return the user's GraphQL id as {'id': ...} for comparison against commit author user."""
    query = '''
    query($login: String!){
        user(login: $login) {
            id
        }
    }'''
    variables = {'login': username}
    request = simple_request('user_getter', query, variables)
    return {'id': request.json()['data']['user']['id']}


def follower_getter(username):
    """Return follower count for the given user."""
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    variables = {'login': username}
    request = simple_request('follower_getter', query, variables)
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """Increment the API call counter for a given function id."""
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """Run funct(*args) and return (result, elapsed_seconds)."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    """Print a labeled timing line: '   <name>:       X.XXXX ms' (or 's' if >1s)."""
    label = f"{'   ' + query_type + ':':<23}"
    if difference > 1:
        print(f"{label}{'%.4f' % difference + ' s ':>12}")
    else:
        print(f"{label}{'%.4f' % (difference * 1000) + ' ms':>12}")


def load_config(path='config.toml'):
    """Read and validate config.toml, convert birthday date to datetime."""
    with open(path, 'rb') as f:
        config = tomllib.load(f)
    bd = config['profile']['birthday']
    config['profile']['birthday'] = datetime.datetime(bd.year, bd.month, bd.day)
    return config


def compute_dots(key_len, value_len, target_width):
    """Compute dot-fill string for a key-value line.
    N = target_width - 5 - key_len - value_len, minimum 1 dot.
    The 5 accounts for: '. ' prefix (2) + ':' (1) + space before dots (1) + space after dots (1)."""
    n = max(1, target_width - 5 - key_len - value_len)
    return ' ' + '.' * n + ' '


def build_data_tspans(config, api_data):
    """Build the complete list of tspan elements for the SVG data block."""
    SVG_NS = "http://www.w3.org/2000/svg"
    target = config['layout']['target_width']
    X = "390"
    y = [30]  # mutable for nested functions
    STEP = 20

    def ts(text=None, cls=None, x=None, y_val=None):
        el = etree.Element(f"{{{SVG_NS}}}tspan")
        if x is not None:
            el.set('x', str(x))
        if y_val is not None:
            el.set('y', str(y_val))
        if cls is not None:
            el.set('class', cls)
        if text is not None:
            el.text = text
        return el

    def advance():
        y[0] += STEP

    def stat_dots(val_str, slot):
        just = max(0, slot - len(val_str))
        if just <= 2:
            return {0: '', 1: ' ', 2: '. '}.get(just, '')
        return ' ' + '.' * just + ' '

    def fmt(n):
        return f"{n:,}" if isinstance(n, int) else str(n)

    tspans = []

    # --- line builders ---

    def header_line(title):
        em = target - len(title) - 5
        el = ts(text=title, x=X, y_val=y[0])
        el.tail = f" -{'—' * em}-—-\n"
        tspans.append(el)
        advance()

    def blank_line():
        el = ts(text='. ', cls='cc', x=X, y_val=y[0])
        el.tail = '\n'
        tspans.append(el)
        advance()

    def kv_line(key, value):
        d = compute_dots(len(key), len(value), target)
        prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
        k = ts(text=key, cls='key'); k.tail = ':'
        dots_el = ts(text=d, cls='cc')
        v = ts(text=value, cls='value'); v.tail = '\n'
        tspans.extend([prefix, k, dots_el, v])
        advance()

    def compound_kv_line(section, key, value):
        full_key_len = len(section) + 1 + len(key)
        d = compute_dots(full_key_len, len(value), target)
        prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
        sec = ts(text=section, cls='key'); sec.tail = '.'
        k = ts(text=key, cls='key'); k.tail = ':'
        dots_el = ts(text=d, cls='cc')
        v = ts(text=value, cls='value'); v.tail = '\n'
        tspans.extend([prefix, sec, k, dots_el, v])
        advance()

    def section_break():
        advance()

    # --- build the block ---

    # Header
    username = config['profile']['username']
    hostname = config['profile']['hostname']
    header_line(f"{username}@{hostname}")

    # Info section
    info_items = list(config['info'].items())
    for i, (key, value) in enumerate(info_items):
        kv_line(key, value)
        if i == 0:  # Insert Uptime after first info field (OS)
            kv_line('Uptime', api_data['age'])

    # Blank separator
    blank_line()

    # Languages (compound)
    for key, value in config['languages'].items():
        compound_kv_line('Languages', key, value)

    # Blank separator
    blank_line()

    # Hobbies (compound)
    for key, value in config['hobbies'].items():
        compound_kv_line('Hobbies', key, value)

    # Interests (compound)
    if 'interests' in config:
        for key, value in config['interests'].items():
            compound_kv_line('Interests', key, value)

    # Contact section
    section_break()
    header_line('- Contact')
    for key, value in config['contact'].items():
        kv_line(key, value)

    # GitHub Stats section
    section_break()
    header_line('- GitHub Stats')

    # Repos & Stars / Commits & Followers — compute pipe column so '|' aligns
    repo_val = fmt(api_data['repos'])
    contrib_val = fmt(api_data['contribs'])
    star_val = fmt(api_data['stars'])
    commit_val = fmt(api_data['commits'])
    follower_val = fmt(api_data['followers'])

    # Left-half fixed chars (excluding dots string):
    #   Repos:   '. '(2) + 'Repos'(5) + ':'(1) + dots + repo + ' {'(2) + 'Contrib'(7) + ': '(2) + contrib + '}'(1) = 20 + (n+2) + repo + contrib
    #   Commits: '. '(2) + 'Commits'(7) + ':'(1) + dots + commit = 10 + (n+2) + commit
    # Both left halves must equal pipe_pos.  dots string = ' ' + '.'*n + ' ' (len = n+2).
    pipe_pos = target // 2
    pipe_pos = max(pipe_pos, 23 + len(repo_val) + len(contrib_val))
    pipe_pos = max(pipe_pos, 13 + len(commit_val))
    pipe_pos = min(pipe_pos, target - 12 - len(star_val))
    pipe_pos = min(pipe_pos, target - 16 - len(follower_val))

    def make_dots(n):
        n = max(1, n)
        return ' ' + '.' * n + ' '

    repo_dots_n = pipe_pos - 22 - len(repo_val) - len(contrib_val)
    commit_dots_n = pipe_pos - 12 - len(commit_val)
    # Right half: target - pipe_pos - 3(' | ')
    star_dots_n = target - pipe_pos - 11 - len(star_val)
    follower_dots_n = target - pipe_pos - 15 - len(follower_val)

    # Repos & Stars line
    prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
    repos_key = ts(text='Repos', cls='key'); repos_key.tail = ':'
    repos_dots = ts(text=make_dots(repo_dots_n), cls='cc')
    repos_val = ts(text=repo_val, cls='value'); repos_val.tail = ' {'
    contrib_key = ts(text='Contrib', cls='key'); contrib_key.tail = ': '
    contrib_v = ts(text=contrib_val, cls='value'); contrib_v.tail = '} | '
    stars_key = ts(text='Stars', cls='key'); stars_key.tail = ':'
    stars_dots = ts(text=make_dots(star_dots_n), cls='cc')
    stars_val = ts(text=star_val, cls='value'); stars_val.tail = '\n'
    tspans.extend([prefix, repos_key, repos_dots, repos_val,
                   contrib_key, contrib_v, stars_key, stars_dots, stars_val])
    advance()

    # Commits & Followers line
    prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
    commits_key = ts(text='Commits', cls='key'); commits_key.tail = ':'
    commits_dots = ts(text=make_dots(commit_dots_n), cls='cc')
    commits_val = ts(text=commit_val, cls='value'); commits_val.tail = ' | '
    followers_key = ts(text='Followers', cls='key'); followers_key.tail = ':'
    followers_dots = ts(text=make_dots(follower_dots_n), cls='cc')
    followers_val = ts(text=follower_val, cls='value'); followers_val.tail = '\n'
    tspans.extend([prefix, commits_key, commits_dots, commits_val,
                   followers_key, followers_dots, followers_val])
    advance()

    # LOC line
    loc_net = api_data['loc_net']
    loc_add = api_data['loc_add']
    loc_del = api_data['loc_del']

    prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
    loc_key = ts(text='GitHub LOC', cls='key'); loc_key.tail = ':'
    # Fixed: '. '(2) + key(10) + ':'(1) + ' '(1) + dots + ' '(1) + loc_net + ' ( '(3) + loc_add + '++'(2) + ', '(2) + loc_del + '--'(2) + ' )'(2) = 26 + dots_n + values
    loc_dots_n = max(1, target - 26 - len(loc_net) - len(loc_add) - len(loc_del))
    loc_dots = ts(text=' ' + '.' * loc_dots_n + ' ', cls='cc')
    loc_val = ts(text=loc_net, cls='value'); loc_val.tail = ' ( '
    add_val = ts(text=loc_add, cls='addColor')
    add_pp = ts(text='++', cls='addColor'); add_pp.tail = ', '
    del_val = ts(text=loc_del, cls='delColor')
    del_pp = ts(text='--', cls='delColor'); del_pp.tail = ' )\n'
    tspans.extend([prefix, loc_key, loc_dots, loc_val,
                   add_val, add_pp, del_val, del_pp])
    advance()

    return tspans


def update_svg(filename, tspans):
    """Parse SVG, find data-block <text> element, replace its children with generated tspans."""
    tree = etree.parse(filename)
    root = tree.getroot()
    ns = root.nsmap.get(None, '')

    data_text = root.find(f".//{{{ns}}}text[@id='data-block']")
    if data_text is None:
        raise ValueError(f"Could not find data-block text element in {filename}")

    # Clear existing children and text
    for child in list(data_text):
        data_text.remove(child)
    data_text.text = '\n'

    # Append generated tspans
    for t in tspans:
        data_text.append(t)

    tree.write(filename, encoding='utf-8', xml_declaration=True)


def main():
    """Fetch all stats, build the SVG data block, and write both theme SVGs."""
    global OWNER_ID

    config = load_config()

    print('Calculation times:')

    OWNER_ID, user_time = perf_counter(user_getter, USER_NAME)
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, config['profile']['birthday'])
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    if total_loc[-1]:
        formatter('LOC (cached)', loc_time)
    else:
        formatter('LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter)
    (repo_data, star_data), repo_star_time = perf_counter(graph_repos_stars, 'both', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    if ENABLE_ARCHIVE:
        try:
            archived_data = add_archive()
            for index in range(len(total_loc)-1):
                total_loc[index] += archived_data[index]
            contrib_data += archived_data[-1]
            commit_data += int(archived_data[-2])
        except (KeyError, IndexError, ValueError, TypeError) as e:
            print(f"Error merging archive data: {e}")

    for index in range(len(total_loc)-1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    api_data = {
        'age': age_data,
        'commits': commit_data,
        'stars': star_data,
        'repos': repo_data,
        'contribs': contrib_data,
        'followers': follower_data,
        'loc_add': total_loc[0],
        'loc_del': total_loc[1],
        'loc_net': total_loc[2],
    }

    # deepcopy because lxml elements can only belong to one tree
    tspans = build_data_tspans(config, api_data)
    update_svg('dark_mode.svg', [copy.deepcopy(t) for t in tspans])
    update_svg('light_mode.svg', tspans)

    total_time = user_time + age_time + loc_time + commit_time + repo_star_time + contrib_time + follower_time

    print('\nSummary:')
    print(f"Total function time: {total_time:.4f} s")
    print(f"Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values())}")

    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name}: {count:>6}")


if __name__ == '__main__':
    main()
