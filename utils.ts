import { createHash } from "node:crypto";
export type Item = Record<string, any>;
export const json = (value: any): string =>
  Array.isArray(value)
    ? `[${value.map(json).join(",")}]`
    : value && typeof value === "object"
      ? `{${Object.keys(value)
          .sort()
          .filter((key) => value[key] !== undefined)
          .map((key) => `${JSON.stringify(key)}:${json(value[key])}`)
          .join(",")}}`
      : (JSON.stringify(value) ?? "null");

export const hash = (value: any) =>
  createHash("sha256")
    .update(typeof value === "string" ? value : json(value))
    .digest("hex");

export const image = (value: any) =>
  value &&
  typeof value === "object" &&
  ["image", "input_image", "output_image", "image_url"].includes(value.type);
export const text = (value: any): string =>
  typeof value === "string"
    ? value
    : Array.isArray(value)
      ? value.map(text).filter(Boolean).join("\n")
      : image(value)
        ? "[image]"
        : value && typeof value === "object"
          ? text(value.text ?? value.content ?? value.output ?? "")
          : value == null
            ? ""
            : String(value);

export const tokens = (value: any): number =>
  Math.ceil(
    Array.from(typeof value === "string" ? value : json(stripImages(value)))
      .length / 3.5,
  );
function stripImages(value: any): any {
  return image(value)
    ? null
    : Array.isArray(value)
      ? value.filter((v) => !image(v)).map(stripImages)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.entries(value).map(([key, v]) => [key, stripImages(v)]),
          )
        : value;
}
export function turns(items: Item[]): [Item[], Item[][]] {
  const prefix: Item[] = [],
    groups: Item[][] = [];
  for (const item of items) {
    if (!groups.length && ["system", "developer"].includes(item.role)) {
      prefix.push(item);
      continue;
    }
    const resultOnly =
      item.role === "user" &&
      Array.isArray(item.content) &&
      item.content.length &&
      item.content.every((part: any) => part?.type === "tool_result");
    if (!groups.length || (item.role === "user" && !resultOnly))
      groups.push([]);
    groups.at(-1)!.push(item);
  }
  return [prefix, groups];
}
export function transcript(block: Item[]): string {
  const sections: string[] = [];
  for (const item of block) {
    const role = item.role,
      type = item.type;
    if (type === "function_call")
      sections.push(
        `tool_call:\n${item.name}(${typeof item.arguments === "string" ? item.arguments : json(item.arguments)})`,
      );
    else if (
      type === "function_call_output" ||
      role === "tool" ||
      role === "toolResult"
    )
      sections.push(`tool_output:\n${text(item.output ?? item.content)}`);
    else if (type === "reasoning")
      sections.push(`thinking:\n${text(item.summary ?? item.content)}`);
    else {
      const parts = Array.isArray(item.content)
        ? item.content
        : [{ type: "text", text: item.content }];
      const visible = parts.filter(
        (part: any) =>
          !["tool_use", "tool_result", "thinking"].includes(part?.type),
      );
      if (text(visible))
        sections.push(`${role ?? "assistant"}:\n${text(visible)}`);
      for (const part of parts) {
        if (part?.type === "tool_use")
          sections.push(`tool_call:\n${part.name}(${json(part.input)})`);
        if (part?.type === "tool_result")
          sections.push(`tool_output:\n${text(part.content)}`);
        if (part?.type === "thinking")
          sections.push(`thinking:\n${part.thinking ?? part.text ?? ""}`);
      }
      for (const call of item.tool_calls ?? [])
        sections.push(
          `tool_call:\n${call.function?.name ?? call.name}(${typeof call.function?.arguments === "string" ? call.function.arguments : json(call.function?.arguments ?? call.arguments)})`,
        );
      if (item.function_call)
        sections.push(
          `tool_call:\n${item.function_call.name}(${text(item.function_call.arguments)})`,
        );
      if (item.reasoning_content ?? item.reasoning)
        sections.push(
          `thinking:\n${text(item.reasoning_content ?? item.reasoning)}`,
        );
    }
  }
  const order = ["user", "assistant", "tool_call", "tool_output", "thinking"];
  const grouped = order
    .map((role) => {
      const matching = sections.filter((section) =>
        section.startsWith(role + ":\n"),
      );
      return matching.length
        ? role +
            ":\n" +
            matching.map((section) => section.slice(role.length + 2)).join("\n")
        : "";
    })
    .filter(Boolean);
  return grouped.join("\n---\n") + "\n---\n---------------------------";
}
