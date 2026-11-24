/**
 * Pi / Testy extension: detect Slack URLs in submitted user prompts and inject
 * a concise hidden instruction reminding the agent to read them via `slack-read`
 * instead of opening Slack in a browser.
 */

const SLACK_URL_RE =
	/https?:\/\/(?:(?:[a-z0-9-]+\.)*slack\.com\/archives\/[A-Z0-9]+(?:\/p\d{10,20})?(?:\?[^\s<>"'`)\]]*)?|app\.slack\.com\/client\/[A-Z0-9]+\/[A-Z0-9]+(?:\/[^\s<>"'`)\]]*)?)/gi;

const TRAILING_PUNCT_RE = /[.,;:!?)>\]}'"`]+$/;

function shellQuote(arg) {
	return `'${String(arg).replace(/'/g, `'\\''`)}'`;
}

export function extractSlackUrls(text) {
	if (typeof text !== "string" || !text.toLowerCase().includes("slack.com/")) {
		return [];
	}
	const seen = new Set();
	const urls = [];
	SLACK_URL_RE.lastIndex = 0;
	for (const match of text.matchAll(SLACK_URL_RE)) {
		const url = match[0].replace(TRAILING_PUNCT_RE, "");
		if (url && !seen.has(url)) {
			seen.add(url);
			urls.push(url);
		}
	}
	return urls;
}

export function buildHint(urls) {
	const quoted = urls.map(shellQuote).join(" ");
	return [
		"Slack URL(s) detected in the user prompt. Use the `slack-read` CLI (read-only Slack Web API helper; skill `slack-read`) instead of opening Slack in a browser to read them.",
		`Run: slack-read ${quoted}`,
		"Defaults: recovers thread root even from reply links, paginates all cursors, expands sub-threads, deduplicates repeated thread unfurls, and fetches up to 20 follow-up messages for DMs (`D...`) up to a 30-minute gap. Add `--after 20` or `--before 10` for channel context (or to read past a DM gap) if needed. Only fall back to the browser to send messages as the user or if a required OAuth scope is missing.",
	].join("\n");
}

export default function slackReadPromptExtension(pi) {
	pi.on("before_agent_start", async (event) => {
		if (!event || event.messageType !== "user") {
			return;
		}
		const urls = extractSlackUrls(event.prompt);
		if (urls.length === 0) {
			return;
		}
		return {
			message: {
				customType: "slack-read-hint",
				content: buildHint(urls),
				display: false,
				details: { urls },
			},
		};
	});
}
