import datetime
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
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
ENABLE_ARCHIVE = os.environ.get('ENABLE_ARCHIVE', 'true').lower() == 'true'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 
               'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
OWNER_ID = None


def daily_readme(birthday):
    """
    Returns the length of time since the given birthday
    e.g. 'XX years, XX months, XX days'
    
    Args:
        birthday: datetime object representing the birthday
        
    Returns:
        str: Formatted age string
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted plural suffix
    
    Args:
        unit: The quantity to check for plurality
        
    Returns:
        str: 's' if unit is not 1, otherwise empty string
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Sends a GraphQL request to GitHub API with error handling
    
    Args:
        func_name: Name of the calling function for error reporting
        query: GraphQL query string
        variables: Variables for the GraphQL query
        
    Returns:
        Response object from requests
        
    Raises:
        Exception: If request fails
    """
    query_count(func_name)
    try:
        request = requests.post('https://api.github.com/graphql', 
                               json={'query': query, 'variables': variables}, 
                               headers=HEADERS)
        if request.status_code == 200:
            return request
        
        error_msg = f"{func_name} failed with status {request.status_code}: {request.text}"
        raise Exception(error_msg)
    except requests.RequestException as e:
        raise Exception(f"{func_name} request failed: {e}")


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return total commit count
    
    Args:
        start_date: Start date for commit range
        end_date: End date for commit range
        
    Returns:
        int: Total number of commits
    """
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {
        'start_date': start_date,
        'end_date': end_date, 
        'login': USER_NAME
    }
    request = simple_request('graph_commits', query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return repository or star count
    
    Args:
        count_type: 'repos' or 'stars' to determine what to count
        owner_affiliation: Repository affiliation filter
        cursor: Pagination cursor
        
    Returns:
        int: Count of repositories or stars
    """
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
    variables = {
        'owner_affiliation': owner_affiliation, 
        'login': USER_NAME, 
        'cursor': cursor
    }
    request = simple_request('graph_repos_stars', query, variables)
    
    if count_type == 'repos':
        # Directly use totalCount from GitHub API for repository count
        return request.json()['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        # For stars, we still need to count manually across all repositories
        result = stars_counter(request.json()['data']['user']['repositories']['edges'])
        
        # Check if there are more pages of repositories to fetch for star counting
        if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:
            next_cursor = request.json()['data']['user']['repositories']['pageInfo']['endCursor']
            # Recursively call to get stars from next page and add to current result
            result += graph_repos_stars('stars', owner_affiliation, next_cursor)
            
        return result


def stars_counter(data):
    """
    Count total stars in repositories
    
    Args:
        data: List of repository data
        
    Returns:
        int: Total star count
    """
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to fetch commit data with pagination
    
    Args:
        owner: Repository owner
        repo_name: Repository name
        data: Cache data
        cache_comment: Comment data for cache file
        addition_total: Running total of line additions
        deletion_total: Running total of line deletions
        my_commits: Running total of my commits
        cursor: Pagination cursor
        
    Returns:
        tuple: (additions, deletions, commit_count)
    """
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
    
    try:
        # We can't use simple_request here because we need to handle the file specially
        query_count('recursive_loc')
        request = requests.post('https://api.github.com/graphql', 
                               json={'query': query, 'variables': variables}, 
                               headers=HEADERS)
        
        if request.status_code == 200:
            if request.json()['data']['repository']['defaultBranchRef'] is not None:
                return loc_counter_one_repo(
                    owner, repo_name, data, cache_comment,
                    request.json()['data']['repository']['defaultBranchRef']['target']['history'],
                    addition_total, deletion_total, my_commits
                )
            else:
                return 0, 0, 0
                
        force_close_file(data, cache_comment)
        if request.status_code == 403:
            raise Exception('Too many requests in a short amount of time! You\'ve hit the non-documented anti-abuse limit!')
        raise Exception(f'recursive_loc() failed with status {request.status_code}: {request.text}')
        
    except Exception as e:
        force_close_file(data, cache_comment)
        raise Exception(f"Error in recursive_loc: {e}")


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Process commit history for a repository
    
    Args:
        owner: Repository owner
        repo_name: Repository name
        data: Cache data
        cache_comment: Comment data for cache file
        history: Commit history data
        addition_total: Running total of line additions
        deletion_total: Running total of line deletions
        my_commits: Running total of my commits
        
    Returns:
        tuple: (additions, deletions, commit_count)
    """
    global OWNER_ID
    
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(
            owner, repo_name, data, cache_comment,
            addition_total, deletion_total, my_commits,
            history['pageInfo']['endCursor']
        )


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Query all repositories to calculate lines of code statistics
    
    Args:
        owner_affiliation: Repository affiliation filter
        comment_size: Number of comment lines in cache file
        force_cache: Whether to force rebuilding the cache
        cursor: Pagination cursor
        edges: List of repository edges
        
    Returns:
        list: [additions, deletions, net, cached_status]
    """
    if edges is None:
        edges = []
        
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
    variables = {
        'owner_affiliation': owner_affiliation, 
        'login': USER_NAME, 
        'cursor': cursor
    }
    request = simple_request('loc_query', query, variables)
    
    repo_data = request.json()['data']['user']['repositories']
    edges += repo_data['edges']
    
    if repo_data['pageInfo']['hasNextPage']:
        return loc_query(
            owner_affiliation, 
            comment_size, 
            force_cache, 
            repo_data['pageInfo']['endCursor'], 
            edges
        )
    else:
        return cache_builder(edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Builds and maintains the cache of repository data
    
    Args:
        edges: List of repository edges
        comment_size: Number of comment lines in cache file
        force_cache: Whether to force rebuilding the cache
        loc_add: Running total of line additions
        loc_del: Running total of line deletions
        
    Returns:
        list: [additions, deletions, net, cached_status]
    """
    cached = True  # Assume all repositories are cached
    filename = get_cache_filename(USER_NAME)
    
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]  # save the comment block
    data = data[comment_size:]  # remove those lines
    
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        repo_name = edges[index]['node']['nameWithOwner']
        repo_name_hash = hashlib.sha256(repo_name.encode('utf-8')).hexdigest()
        
        if repo_hash == repo_name_hash:
            try:
                current_commit_count = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
                if int(commit_count) != current_commit_count:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = repo_name.split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = f"{repo_hash} {current_commit_count} {loc[2]} {loc[0]} {loc[1]}\n"
            except TypeError:  # If the repo is empty
                data[index] = f"{repo_hash} 0 0 0 0\n"
                
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
        
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
        
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes and reinitializes the cache file
    
    Args:
        edges: List of repository edges
        filename: Path to cache file
        comment_size: Number of comment lines to preserve
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]  # only save the comment
            
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            repo_name = node['node']['nameWithOwner']
            repo_hash = hashlib.sha256(repo_name.encode('utf-8')).hexdigest()
            f.write(f"{repo_hash} 0 0 0 0\n")


def get_cache_filename(username):
    """
    Generate a unique cache filename for the user
    
    Args:
        username: GitHub username
        
    Returns:
        str: Cache filename
    """
    os.makedirs('cache', exist_ok=True)
    return f"cache/{hashlib.sha256(username.encode('utf-8')).hexdigest()}.txt"


def add_archive():
    """
    Add statistics from archived repositories
    
    Returns:
        list: [additions, deletions, net, commit_count, repo_count]
    """
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
    except Exception as e:
        print(f"Error processing archive data: {e}")
        return [0, 0, 0, 0, 0]


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving data before crash
    
    Args:
        data: Cache data to save
        cache_comment: Comment data for cache file
    """
    filename = get_cache_filename(USER_NAME)
    try:
        with open(filename, 'w') as f:
            f.writelines(cache_comment)
            f.writelines(data)
        print(f'Saved partial data to {filename} before error.')
    except Exception as e:
        print(f"Error saving cache file: {e}")


def commit_counter(comment_size):
    """
    Counts total commits from cache file
    
    Args:
        comment_size: Number of comment lines in cache file
        
    Returns:
        int: Total commit count
    """
    total_commits = 0
    filename = get_cache_filename(USER_NAME)
    
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
            
        # Skip comment lines and process data lines
        data = data[comment_size:]
        for line in data:
            parts = line.split()
            if len(parts) >= 3:
                total_commits += int(parts[2])
        return total_commits
    except FileNotFoundError:
        print(f"Warning: Cache file {filename} not found. Returning 0 commits.")
        return 0
    except Exception as e:
        print(f"Error counting commits: {e}")
        return 0


def user_getter(username):
    """
    Get user ID and account creation date
    
    Args:
        username: GitHub username
        
    Returns:
        tuple: (user_id_dict, created_at_date)
    """
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request('user_getter', query, variables)
    response_data = request.json()['data']['user']
    return {'id': response_data['id']}, response_data['createdAt']


def follower_getter(username):
    """
    Get follower count for a user
    
    Args:
        username: GitHub username
        
    Returns:
        int: Follower count
    """
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
    """
    Track API usage
    
    Args:
        funct_id: Function identifier
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Measure function performance
    
    Args:
        funct: Function to measure
        *args: Arguments to pass to function
        
    Returns:
        tuple: (function_result, execution_time)
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Format performance measurement output
    
    Args:
        query_type: Name of the query
        difference: Time measurement
        funct_return: Function return value (optional)
        whitespace: Whitespace padding (optional)
        
    Returns:
        Function return value or formatted string
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
        
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


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
    #   Repos:   '. '(2) + 'Repos'(5) + ':'(1) + dots + repo + ' {'(2) + 'Contributed'(11) + ': '(2) + contrib + '}'(1) = 24 + (n+2) + repo + contrib
    #   Commits: '. '(2) + 'Commits'(7) + ':'(1) + dots + commit = 10 + (n+2) + commit
    # Both left halves must equal pipe_pos.  dots string = ' ' + '.'*n + ' ' (len = n+2).
    pipe_pos = max(27 + len(repo_val) + len(contrib_val),
                   13 + len(commit_val))

    def make_dots(n):
        n = max(1, n)
        return ' ' + '.' * n + ' '

    repo_dots_n = pipe_pos - 26 - len(repo_val) - len(contrib_val)
    commit_dots_n = pipe_pos - 12 - len(commit_val)
    # Right half: target - pipe_pos - 3(' | ')
    star_dots_n = target - pipe_pos - 11 - len(star_val)
    follower_dots_n = target - pipe_pos - 15 - len(follower_val)

    # Repos & Stars line
    prefix = ts(text='. ', cls='cc', x=X, y_val=y[0])
    repos_key = ts(text='Repos', cls='key'); repos_key.tail = ':'
    repos_dots = ts(text=make_dots(repo_dots_n), cls='cc')
    repos_val = ts(text=repo_val, cls='value'); repos_val.tail = ' {'
    contrib_key = ts(text='Contributed', cls='key'); contrib_key.tail = ': '
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
    """Main function to run the GitHub statistics update"""
    global OWNER_ID

    # Load config
    config = load_config()

    print('Calculation times:')

    # Get user data and account creation date
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    # Calculate age using config birthday
    age_data, age_time = perf_counter(daily_readme, config['profile']['birthday'])
    formatter('age calculation', age_time)

    # Get lines of code statistics
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    if total_loc[-1]:
        formatter('LOC (cached)', loc_time)
    else:
        formatter('LOC (no cache)', loc_time)

    # Get other GitHub statistics
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # Add archived repository data if enabled
    if ENABLE_ARCHIVE:
        try:
            archived_data = add_archive()
            for index in range(len(total_loc)-1):
                total_loc[index] += archived_data[index]
            contrib_data += archived_data[-1]  # Add archived repos to contributed repos count
            commit_data += int(archived_data[-2])
        except Exception as e:
            print(f"Error adding archive data: {e}")

    # Format LOC numbers
    for index in range(len(total_loc)-1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    # Pack API data
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

    # Update SVG files (build tspans twice since lxml elements can only belong to one tree)
    update_svg('dark_mode.svg', build_data_tspans(config, api_data))
    update_svg('light_mode.svg', build_data_tspans(config, api_data))

    # Calculate and display total execution time
    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time

    # Use a more cross-platform way to display summary
    print('\nSummary:')
    print(f"Total function time: {total_time:.4f} s")
    print(f"Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values())}")

    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name}: {count:>6}")


if __name__ == '__main__':
    main()
