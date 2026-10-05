import net from "node:net";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

type ArtifactKind = "file" | "image";

interface ParentResponse {
	id: string;
	accepted: boolean;
	message: string;
	reason?: string;
}

const MAX_FRAME_BYTES = 16 * 1024;
const REQUEST_TIMEOUT_MS = 30_000;

function requireEnv(name: string): string {
	const value = process.env[name];
	if (!value) throw new Error("Artifact broker is unavailable");
	return value;
}

function requestParent(
	request: Record<string, unknown>,
	signal: AbortSignal,
): Promise<ParentResponse> {
	const socketPath = requireEnv("TELEGRAM_PI_ARTIFACT_SOCKET");
	const payload = Buffer.from(`${JSON.stringify(request)}\n`, "utf8");
	if (payload.length > MAX_FRAME_BYTES) {
		return Promise.reject(new Error("Artifact request is too large"));
	}

	return new Promise((resolve, reject) => {
		let settled = false;
		let buffered = Buffer.alloc(0);
		const socket = net.createConnection({ path: socketPath });
		const timer = setTimeout(
			() => finish(new Error("Artifact broker timed out")),
			REQUEST_TIMEOUT_MS,
		);

		function cleanup() {
			clearTimeout(timer);
			signal.removeEventListener("abort", onAbort);
			socket.removeAllListeners();
			socket.destroy();
		}

		function finish(error?: Error, response?: ParentResponse) {
			if (settled) return;
			settled = true;
			cleanup();
			if (error) reject(error);
			else resolve(response as ParentResponse);
		}

		function onAbort() {
			finish(new Error("Artifact request was aborted"));
		}

		signal.addEventListener("abort", onAbort, { once: true });
		if (signal.aborted) {
			onAbort();
			return;
		}

		socket.once("connect", () => socket.write(payload));
		socket.once("error", () => finish(new Error("Artifact broker failed")));
		socket.once("end", () => {
			if (!settled) finish(new Error("Artifact broker disconnected"));
		});
		socket.on("data", (chunk) => {
			buffered = Buffer.concat([buffered, chunk]);
			if (buffered.length > MAX_FRAME_BYTES) {
				finish(new Error("Artifact response is too large"));
				return;
			}
			const newline = buffered.indexOf(0x0a);
			if (newline < 0) return;
			try {
				const value = JSON.parse(buffered.subarray(0, newline).toString("utf8"));
				if (
					typeof value !== "object" ||
					value === null ||
					value.id !== request.id ||
					typeof value.accepted !== "boolean" ||
					typeof value.message !== "string"
				) {
					throw new Error("invalid response");
				}
				finish(undefined, value as ParentResponse);
			} catch {
				finish(new Error("Artifact broker returned an invalid response"));
			}
		});
	});
}

function artifactTool(kind: ArtifactKind) {
	return defineTool({
		name: kind === "file" ? "send_file" : "send_image",
		label: kind === "file" ? "Send file" : "Send image",
		description: `Queue one validated ${kind} for delivery to the authorized Telegram chat.`,
		parameters: Type.Object({
			path: Type.String({
				description: "Absolute path or path relative to Pi's configured working directory",
			}),
			caption: Type.Optional(
				Type.String({ description: "Short Telegram caption" }),
			),
		}),
		async execute(toolCallId, params, signal) {
			let response: ParentResponse;
			try {
				response = await requestParent(
					{
						id: toolCallId,
						capability: requireEnv("TELEGRAM_PI_ARTIFACT_CAPABILITY"),
						kind,
						path: params.path,
						caption: params.caption ?? "",
					},
					signal,
				);
			} catch (error) {
				response = {
					id: toolCallId,
					accepted: false,
					message: "Artifact broker rejected or could not confirm the request.",
					reason:
						error instanceof Error ? error.message : "Artifact broker failed",
				};
			}
			return {
				content: [{ type: "text" as const, text: response.message }],
				details: response,
				isError: !response.accepted,
			};
		},
	});
}

export default function telegramArtifacts(pi: ExtensionAPI) {
	pi.registerTool(artifactTool("file"));
	pi.registerTool(artifactTool("image"));
}
