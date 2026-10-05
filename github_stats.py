#!/usr/bin/python3

import asyncio
import json
import os
from typing import Dict, List, Optional, Set, Tuple, Union

import aiohttp


###############################################################################
# Main Classes
###############################################################################

class GitHubAPIError(RuntimeError):
    """An incomplete API response that must not be published as zero stats."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(self, username: str, access_token: str,
                 session: aiohttp.ClientSession, max_connections: int = 10):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }
        try:
            async with self.semaphore:
                async with self.session.post(
                    "https://api.github.com/graphql", headers=headers,
                    json={"query": generated_query},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status != 200:
                        raise GitHubAPIError(
                            f"GitHub GraphQL request failed (HTTP {r.status}).",
                            r.status,
                        )
                    result = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # Do not expose API URLs or response bodies containing private repos.
            raise GitHubAPIError("Could not read a GitHub GraphQL response.") from None

        if (not isinstance(result, dict) or result.get("errors")
                or not isinstance(result.get("data"), dict)):
            raise GitHubAPIError("GitHub returned incomplete GraphQL data.")
        return result

    async def query_rest(self, path: str, params: Optional[Dict] = None
                         ) -> Optional[Union[Dict, List]]:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :return: deserialized REST JSON output
        """

        headers = {"Authorization": f"token {self.access_token}"}
        path = path.lstrip("/")
        for _ in range(60):
            try:
                async with self.semaphore:
                    async with self.session.get(
                        f"https://api.github.com/{path}", headers=headers,
                        params=params or {}, timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        if r.status == 204:
                            return None
                        if r.status == 200:
                            return await r.json()
                        if r.status != 202:
                            raise GitHubAPIError(
                                f"GitHub REST request failed (HTTP {r.status}).",
                                r.status,
                            )
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                raise GitHubAPIError("Could not read a GitHub REST response.") from None
            await asyncio.sleep(2)

        raise GitHubAPIError("GitHub statistics are still being computed.", 202)

    @staticmethod
    def repos_overview(contrib_cursor: Optional[str] = None,
                       owned_cursor: Optional[str] = None) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    id,
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        isEmpty
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        isEmpty
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def commit_history(repo: str, user_id: str,
                       cursor: Optional[str] = None) -> str:
        """Read authored commits using the account's stable GraphQL node ID."""
        owner, name = repo.split("/", 1)
        return f"""{{
  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
    defaultBranchRef {{
      target {{
        ... on Commit {{
          history(first: 100, after: {json.dumps(cursor)},
                  author: {{id: {json.dumps(user_id)}}}) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              additions
              deletions
              parents {{ totalCount }}
            }}
          }}
        }}
      }}
    }}
  }}
}}"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """
    def __init__(self, username: str, access_token: str,
                 session: aiohttp.ClientSession,
                 exclude_repos: Optional[Set] = None,
                 exclude_langs: Optional[Set] = None,
                 consider_forked_repos: bool = False):
        self.username = username
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self._consider_forked_repos = consider_forked_repos
        self.queries = Queries(username, access_token, session)

        self._name = None
        self._user_id = None
        self._stargazers = None
        self._forks = None
        self._total_contributions = None
        self._languages = None
        self._repos = None
        self._ignored_repos = None
        self._empty_repos = set()
        self._lines_changed = None
        self._views = None        

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.all_repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        self._stargazers = 0
        self._forks = 0
        self._languages = dict()
        self._repos = set()
        self._ignored_repos = set()
        self._empty_repos = set()
        
        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(owned_cursor=next_owned,
                                       contrib_cursor=next_contrib)
            )
            raw_results = raw_results if raw_results is not None else {}

            viewer = raw_results.get("data", {}).get("viewer")
            if (not isinstance(viewer, dict)
                    or not isinstance(viewer.get("id"), str) or not viewer["id"]
                    or not viewer.get("login")):
                raise GitHubAPIError("Could not identify the account being measured.")
            # The token owner is also the subject of all other statistics.
            # Account IDs survive renames; workflow actors and cached logins do not.
            self._user_id = viewer["id"]
            self.username = viewer["login"]

            self._name = (raw_results
                          .get("data", {})
                          .get("viewer", {})
                          .get("name", None))
            if self._name is None:
                self._name = (raw_results
                              .get("data", {})
                              .get("viewer", {})
                              .get("login", "No Name"))

            contrib_repos = (raw_results
                             .get("data", {})
                             .get("viewer", {})
                             .get("repositoriesContributedTo", {}))
            owned_repos = (raw_results
                           .get("data", {})
                           .get("viewer", {})
                           .get("repositories", {}))

            for repo in owned_repos.get("nodes", []) + contrib_repos.get("nodes", []):
                if repo.get("isEmpty"):
                    self._empty_repos.add(repo["nameWithOwner"])
            
            repos = owned_repos.get("nodes", [])
            if self._consider_forked_repos:
                repos += contrib_repos.get("nodes", [])
            else:
                for repo in contrib_repos.get("nodes", []):
                    name = repo.get("nameWithOwner")
                    if name in self._ignored_repos or name in self._exclude_repos:
                        continue
                    self._ignored_repos.add(name)

            for repo in repos:
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                self._repos.add(name)
                self._stargazers += repo.get("stargazers").get("totalCount", 0)
                self._forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    name = lang.get("node", {}).get("name", "Other")
                    languages = await self.languages
                    if name in self._exclude_langs: continue
                    if name in languages:
                        languages[name]["size"] += lang.get("size", 0)
                        languages[name]["occurrences"] += 1
                    else:
                        languages[name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color")
                        }

            if owned_repos.get("pageInfo", {}).get("hasNextPage", False) or \
                    contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = (owned_repos
                              .get("pageInfo", {})
                              .get("endCursor", next_owned))
                next_contrib = (contrib_repos
                                .get("pageInfo", {})
                                .get("endCursor", next_contrib))
            else:
                break

        # TODO: Improve languages to scale by number of contributions to
        #       specific filetypes
        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert(self._name is not None)
        return self._name

    @property
    async def stargazers(self) -> int:
        """
        :return: total number of stargazers on user's repos
        """
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert(self._stargazers is not None)
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert(self._forks is not None)
        return self._forks

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert(self._languages is not None)
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if self._languages is None:
            await self.get_stats()
            assert(self._languages is not None)

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> List[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert(self._repos is not None)
        return self._repos
    
    @property
    async def all_repos(self) -> List[str]:
        """
        :return: list of names of user's repos with contributed repos included
                irrespective of whether the ignore flag is set or not
        """
        if self._repos is not None and self._ignored_repos is not None:
            return self._repos | self._ignored_repos
        await self.get_stats()
        assert(self._repos is not None)
        assert(self._ignored_repos is not None)
        return self._repos | self._ignored_repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        years = (await self.queries.query(Queries.contrib_years())) \
            .get("data", {}) \
            .get("viewer", {}) \
            .get("contributionsCollection", {}) \
            .get("contributionYears", [])
        by_year = (await self.queries.query(Queries.all_contribs(years))) \
            .get("data", {}) \
            .get("viewer", {}).values()
        for year in by_year:
            self._total_contributions += year \
                .get("contributionCalendar", {}) \
                .get("totalContributions", 0)
        return self._total_contributions

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        """
        if self._lines_changed is not None:
            return self._lines_changed
        repos = await self.all_repos
        # The contributor-statistics endpoint can omit contributions even with
        # HTTP 200. Sum actual commits instead, filtering by stable account ID.
        totals = await asyncio.gather(*(
            self._count_commit_lines(repo)
            for repo in repos if repo not in self._empty_repos
        ))
        self._lines_changed = (
            sum(added for added, _ in totals),
            sum(deleted for _, deleted in totals),
        )
        return self._lines_changed

    async def _count_commit_lines(self, repo: str) -> Tuple[int, int]:
        """Count authored, non-merge commits on a repository's default branch."""
        additions = deletions = 0
        cursor = None
        while True:
            result = await self.queries.query(
                Queries.commit_history(repo, self._user_id, cursor))
            repository = result.get("data", {}).get("repository") or {}
            branch = repository.get("defaultBranchRef") or {}
            history = (branch.get("target") or {}).get("history") or {}
            commits = history.get("nodes")
            page_info = history.get("pageInfo") or {}
            if (not isinstance(commits, list)
                    or not isinstance(page_info.get("hasNextPage"), bool)):
                raise GitHubAPIError("GitHub returned incomplete commit history.")
            for commit in commits:
                if (not isinstance(commit, dict)
                        or not isinstance(commit.get("additions"), int)
                        or not isinstance(commit.get("deletions"), int)
                        or not isinstance(commit.get("parents"), dict)
                        or not isinstance(commit["parents"].get("totalCount"), int)):
                    raise GitHubAPIError("GitHub returned incomplete commit line counts.")
                if commit["parents"]["totalCount"] > 1:
                    continue
                additions += commit["additions"]
                deletions += commit["deletions"]
            if not page_info["hasNextPage"]:
                return additions, deletions
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise GitHubAPIError("GitHub returned incomplete commit pagination.")
            cursor = next_cursor

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views

        total = 0
        for repo in await self.repos:
            if repo in self._empty_repos:
                continue
            try:
                r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
            except GitHubAPIError as error:
                # Traffic is only available with sufficient repository access.
                if error.status in (403, 404):
                    continue
                raise
            if r is None:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("views"), list):
                raise GitHubAPIError("GitHub returned incomplete traffic statistics.")
            for view in r.get("views", []):
                total += view.get("count", 0)

        self._views = total
        return total


###############################################################################
# Main Function
###############################################################################

async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
