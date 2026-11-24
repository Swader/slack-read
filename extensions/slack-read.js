/**
 * Pi / Testy extension: add Slack read instructions to the model request
 * without changing the saved prompt or starting a separate turn.
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
	pi.on("context_with_system", (event) => {
		// Queued input is already in the transcript here. Keep hints through tools
		// and retries, but stop at a prior reply (or failed reply before new input).
		let messages;
		let sawUser = false;
		for (let i = event.messages.length - 1; i >= 0; i--) {
			const message = event.messages[i];
			if (message.role === "assistant" && message.stopReason !== "toolUse"
				&& (sawUser || message.stopReason === "stop")) break;
			// Testy can represent summaries and recovery guidance as labeled user messages.
			if (message.role !== "user" || message.testyContextLabel) continue;
			sawUser = true;
			const content = typeof message.content === "string"
				? [{ type: "text", text: message.content }]
				: message.content;
			const urls = extractSlackUrls(content.filter(block => block.type === "text").map(block => block.text).join("\n"));
			if (urls.length === 0) continue;
			messages ??= event.messages.slice();
			messages[i] = { ...message, content: [...content, { type: "text", text: buildHint(urls) }] };
		}
		if (messages) return { messages };
	});
}
