import { ApiClient, ApiError } from "./client";
import {
  objectPath,
  terminal,
  type JobEvent,
  type JobStatus,
} from "./operations";
export interface ServerEvent {
  id: string;
  event: string;
  data: string;
}
/** Incremental UTF-8 decoding and SSE framing. Acknowledgment follows successful consumption. */
export async function parseEvents(
  body: ReadableStream<Uint8Array>,
  consume: (event: ServerEvent) => Promise<void | boolean>,
  signal: AbortSignal,
  after = "0",
): Promise<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let id = "";
  let data: string[] = [];
  let acknowledged = BigInt(after);
  let ended = false;
  let stopped = false;
  const abort = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  const line = async (value: string) => {
    if (value === "") {
      if (data.length) {
        if (event === "refresh") {
          stopped =
            (await consume({ id: "", event, data: data.join("\n") })) === false;
        } else if (/^\d+$/.test(id) && BigInt(id) > acknowledged) {
          stopped =
            (await consume({
              id,
              event: event || "message",
              data: data.join("\n"),
            })) === false;
          acknowledged = BigInt(id);
        }
      }
      event = "";
      id = "";
      data = [];
      return;
    }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "id" && !content.includes("\0")) id = content;
    else if (field === "event") event = content;
    else if (field === "data") data.push(content);
  };
  try {
    while (!ended && !stopped && !signal.aborted) {
      const chunk = await reader.read();
      ended = chunk.done;
      buffer += decoder.decode(chunk.value, { stream: !ended });
      let index: number;
      while ((index = buffer.search(/[\r\n]/)) >= 0) {
        if (buffer[index] === "\r" && index === buffer.length - 1 && !ended)
          break;
        const size =
          buffer[index] === "\r" && buffer[index + 1] === "\n" ? 2 : 1;
        const value = buffer.slice(0, index);
        buffer = buffer.slice(index + size);
        await line(value);
        if (signal.aborted || stopped) break;
      }
      if (buffer.length > 1048576)
        throw new ApiError(
          "INVALID_RESPONSE",
          "Event frame exceeds local limit.",
        );
    }
    if (signal.aborted) throw new DOMException("Stream ended", "AbortError");
    return acknowledged.toString();
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Stream ended", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}
export async function watchJob(
  client: ApiClient,
  id: string,
  signal: AbortSignal,
  options: {
    onEvent: (event: JobEvent) => Promise<void>;
    refresh: () => Promise<JobStatus>;
    after?: number;
    delay?: (ms: number, signal: AbortSignal) => Promise<void>;
  },
) {
  let sequence = String(options.after ?? 0);
  let finished = false;
  let attempts = 0;
  while (!finished && !signal.aborted) {
    let refresh = false;
    try {
      await client.stream(
        `/v1/jobs/${objectPath(id)}/events`,
        { signal, headers: { "Last-Event-ID": sequence } },
        async (body, activeSignal) => {
          sequence = await parseEvents(
            body,
            async (frame) => {
              if (frame.event === "refresh") {
                refresh = true;
                return false;
              }
              if (frame.event !== "job") return;
              let event: JobEvent;
              try {
                event = JSON.parse(frame.data);
              } catch {
                throw new ApiError("INVALID_RESPONSE", "Malformed job event.");
              }
              if (String(event.sequence) !== frame.id)
                throw new ApiError(
                  "INVALID_RESPONSE",
                  "Job event sequence does not match its cursor.",
                );
              await options.onEvent(event);
              sequence = frame.id;
              finished = terminal(event.state);
              return !finished;
            },
            activeSignal,
            sequence,
          );
        },
      );
      if (finished) return;
      if (refresh) {
        const status = await options.refresh();
        sequence = String(Math.max(Number(sequence), status.latest_sequence));
        finished = terminal(status.state);
        if (finished) return;
      }
      attempts = 0;
    } catch (error) {
      if (
        signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError")
      )
        throw error;
      if (error instanceof ApiError && !error.retryable) throw error;
      if (++attempts >= 5) throw error;
    }
    await (options.delay ?? delay)(Math.min(500 * 2 ** attempts, 8000), signal);
  }
  if (signal.aborted) throw new DOMException("Stream ended", "AbortError");
}
