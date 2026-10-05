import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp

from github_stats import GitHubAPIError, Queries, Stats


def response(status, payload=None):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=Mock(
        status=status, json=AsyncMock(return_value=payload)))
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def overview(empty=False):
    return {"data": {"viewer": {
        "id": "User_7",
        "login": "renamed-user",
        "name": "Example",
        "repositories": {
            "nodes": [{
                "nameWithOwner": "renamed-user/project",
                "isEmpty": empty,
                "stargazers": {"totalCount": 0},
                "forkCount": 0,
                "languages": {"edges": []},
            }],
            "pageInfo": {"hasNextPage": False},
        },
        "repositoriesContributedTo": {
            "nodes": [], "pageInfo": {"hasNextPage": False},
        },
    }}}


def history(commits, has_next=False, cursor=None):
    return {"data": {"repository": {"defaultBranchRef": {"target": {"history": {
        "nodes": commits,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}}}}}


def commit(added, deleted, parents=1):
    return {"additions": added, "deletions": deleted,
            "parents": {"totalCount": parents}}


class QueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Mock()
        self.queries = Queries("actor", "token", self.session)

    async def test_no_content_does_not_parse_json_or_retry(self):
        context = response(204)
        self.session.get.return_value = context
        self.assertIsNone(await self.queries.query_rest("/repos/example/stats"))
        self.session.get.assert_called_once()
        context.__aenter__.return_value.json.assert_not_awaited()

    async def test_pending_statistics_are_retried(self):
        self.session.get.side_effect = [response(202), response(200, [])]
        with patch("github_stats.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.queries.query_rest("/stats"), [])
        sleep.assert_awaited_once_with(2)

    async def test_exhausted_retries_are_not_zero_statistics(self):
        self.session.get.return_value = response(202)
        with patch("github_stats.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(GitHubAPIError) as caught:
                await self.queries.query_rest("/stats")
        self.assertEqual(caught.exception.status, 202)
        self.assertEqual(self.session.get.call_count, 60)

    async def test_rest_error_does_not_return_an_error_object_as_data(self):
        self.session.get.return_value = response(403, {"message": "Forbidden"})
        with self.assertRaises(GitHubAPIError) as caught:
            await self.queries.query_rest("/stats")
        self.assertEqual(caught.exception.status, 403)

    async def test_graphql_partial_errors_stop_collection(self):
        self.session.post.return_value = response(
            200, {"data": {}, "errors": [{"message": "private repository"}]})
        with self.assertRaises(GitHubAPIError) as caught:
            await self.queries.query("{viewer{login}}")
        self.assertNotIn("private repository", str(caught.exception))

    async def test_transport_error_does_not_expose_private_repo_path(self):
        self.session.get.side_effect = aiohttp.ClientError(
            "https://api.github.com/repos/private-owner/private-project")
        with self.assertRaises(GitHubAPIError) as caught:
            await self.queries.query_rest("/repos/private-owner/private-project")
        self.assertNotIn("private-owner", str(caught.exception))


class StatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The actor may be an old login or an automation account.
        self.stats = Stats("workflow-bot", "token", Mock())
        self.stats.queries = Mock(
            query=AsyncMock(return_value=overview()), query_rest=AsyncMock())

    async def test_renamed_account_is_selected_by_stable_id_not_workflow_actor(self):
        self.stats.queries.query.side_effect = [
            overview(), history([commit(10, 4), commit(5, 1)]),
        ]
        self.assertEqual(await self.stats.lines_changed, (15, 5))
        self.assertEqual(self.stats.username, "renamed-user")
        query = self.stats.queries.query.call_args.args[0]
        self.assertIn('author: {id: "User_7"}', query)
        self.assertNotIn("workflow-bot", query)
        self.stats.queries.query_rest.assert_not_awaited()

    async def test_missing_account_id_stops_collection(self):
        data = overview()
        del data["data"]["viewer"]["id"]
        self.stats.queries.query.return_value = data
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed
        self.stats.queries.query_rest.assert_not_awaited()

    async def test_empty_repositories_do_not_request_commit_history(self):
        self.stats.queries.query.return_value = overview(empty=True)
        self.assertEqual(await self.stats.lines_changed, (0, 0))
        self.stats.queries.query.assert_awaited_once()
        self.stats.queries.query_rest.assert_not_awaited()

    async def test_root_and_regular_commits_count_but_merge_commits_do_not(self):
        self.stats.queries.query.side_effect = [
            overview(), history([
                commit(20, 3, parents=0), commit(1000, 1000, parents=2),
                commit(2, 1),
            ]),
        ]
        self.assertEqual(await self.stats.lines_changed, (22, 4))

    async def test_commit_history_paginates_past_first_hundred_commits(self):
        self.stats.queries.query.side_effect = [
            overview(),
            history([commit(1, 2) for _ in range(100)], True, "page-1"),
            history([commit(3, 2)]),
        ]
        self.assertEqual(await self.stats.lines_changed, (103, 202))
        query = self.stats.queries.query.call_args.args[0]
        self.assertIn('after: "page-1"', query)

    async def test_no_authored_commits_is_a_valid_zero(self):
        self.stats.queries.query.side_effect = [overview(), history([])]
        self.assertEqual(await self.stats.lines_changed, (0, 0))

    async def test_failed_history_page_does_not_cache_a_partial_total(self):
        self.stats.queries.query.side_effect = [
            overview(), history([commit(10, 3)], True, "page-1"),
            GitHubAPIError("Unavailable", 503),
        ]
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed
        self.assertIsNone(self.stats._lines_changed)

    async def test_missing_history_does_not_become_zero(self):
        self.stats.queries.query.side_effect = [
            overview(), {"data": {"repository": None}},
        ]
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed

    async def test_missing_line_counts_do_not_become_zero(self):
        self.stats.queries.query.side_effect = [overview(), history([{}])]
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed

    async def test_repeated_pagination_cursor_stops_collection(self):
        self.stats.queries.query.side_effect = [
            overview(), history([commit(1, 1)], True, "page-1"),
            history([commit(1, 1)], True, "page-1"),
        ]
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed

    async def test_missing_traffic_permission_does_not_block_other_statistics(self):
        self.stats.queries.query_rest.side_effect = GitHubAPIError("Forbidden", 403)
        self.assertEqual(await self.stats.views, 0)

    async def test_unexpected_api_failure_stops_collection(self):
        self.stats.queries.query.side_effect = [
            overview(), GitHubAPIError("Unavailable", 503),
        ]
        with self.assertRaises(GitHubAPIError):
            await self.stats.lines_changed


if __name__ == "__main__":
    unittest.main()
