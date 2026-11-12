#!/usr/bin/env node
// UVMC as a tool for Claude Desktop (a desktop extension, .mcpb).
// A relay: MCP messages from Claude Desktop (one JSON per line on stdin) go to POST /mcp on
// the UVMC machine, and the answers come back on stdout. It finds the machine and its panel
// token through Multipass each time it starts, so nothing secret is stored in the extension.
// Only JSON goes to stdout; notes for the log go to stderr. Plain Node, no dependencies.
"use strict";

const { execFile } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const readline = require("readline");

const NAME = process.env.UVMC_MACHINE || "uvmc";
const PORT = 8080;
const log = (text) => process.stderr.write(`uvmc: ${text}\n`);

// Commands the machine didn't answer in time, one line each, the newest 500 kept: the same file
// as UVMC's scripts on this PC (windows\timeouts.ps1, unix/timeouts.sh).
const TIMEOUT_LOG = process.platform === "win32"
  ? path.join(process.env.LOCALAPPDATA || path.join(require("os").homedir(), "AppData", "Local"), "UVMC", "timeouts.log")
  : path.join(require("os").homedir(), ".config", "uvmc", "timeouts.log");
function logTimeout(what, how) {
  try {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    const when = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    fs.mkdirSync(path.dirname(TIMEOUT_LOG), { recursive: true });
    const old = fs.existsSync(TIMEOUT_LOG) ? fs.readFileSync(TIMEOUT_LOG, "utf8").split(/\r?\n/).filter(Boolean).slice(-499) : [];
    fs.writeFileSync(TIMEOUT_LOG, [...old, `${when}  extension  ${what}  ${how}`].join("\n") + "\n");
  } catch { /* never fails the lookup */ }
}

function findMultipass() {
  const candidates = process.platform === "win32"
    ? [path.join(process.env.ProgramFiles || "C:\\Program Files", "Multipass", "bin", "multipass.exe")]
    : ["/usr/local/bin/multipass", "/opt/homebrew/bin/multipass", "/snap/bin/multipass", "/usr/bin/multipass"];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return process.platform === "win32" ? "multipass.exe" : "multipass";   // hope it is on PATH
}

function run(file, args, timeout = 120000) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout, windowsHide: true, maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
      if (err) reject(new Error(`${path.basename(file)} ${args.slice(0, 2).join(" ")}: ${(stderr || err.message).trim()}`));
      else resolve(stdout.trim());
    });
  });
}

// A quick lookup with a limit on both sides: "timeout" in the machine (which stops the command
// and all it started, so the exec ends), and the Multipass client stopped here 10 s later if it
// still hasn't returned. With Multipass on Windows (1.16, still in 1.17.0), an exec started without a console keeps
// a CPU core busy while it runs, and one the machine never answers never ends.
function quick(mp, cmd, limit = 20) {
  const what = cmd.join(" ");
  return new Promise((resolve, reject) => {
    execFile(mp, ["exec", NAME, "--", "timeout", "-k", "5", String(limit), ...cmd],
      { timeout: (limit + 10) * 1000, windowsHide: true, maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
        if (err && err.killed) {
          logTimeout(what, `no answer from Multipass, client stopped after ${limit + 10} s`);
          reject(new Error(`Multipass did not answer within ${limit + 10} seconds (${what}); it was stopped.`));
        } else if (err && (err.code === 124 || err.code === 137)) {
          logTimeout(what, `stopped in the VM after ${limit} s`);
          reject(new Error(`The UVMC machine did not answer within ${limit} seconds (${what}); the command was stopped.`));
        } else if (err) {
          reject(new Error(`multipass exec ${what}: ${(stderr || err.message).trim()}`));
        } else {
          resolve(stdout.trim());
        }
      });
  });
}

async function lookup() {
  // UVMC_URL and UVMC_TOKEN skip the lookup (e.g. UVMC on another machine).
  if (process.env.UVMC_URL && process.env.UVMC_TOKEN) {
    return { base: process.env.UVMC_URL.replace(/\/+$/, ""), token: process.env.UVMC_TOKEN };
  }
  const mp = findMultipass();
  await run(mp, ["start", NAME]).catch((e) => log(`start: ${e.message}`));   // no-op when running
  const ip = (await quick(mp, ["hostname", "-I"])).split(/\s+/)[0];
  const token = await quick(mp, ["sudo", "cat", "/etc/uvmc/panel_token"]);
  if (!ip || !token) throw new Error("Could not find the UVMC machine. Is it installed and running?");
  log(`found the machine at ${ip}`);
  return { base: `http://${ip}:${PORT}`, token };
}

let conn = null;   // a promise of {base, token}, shared by the messages in flight
const connection = () => (conn = conn || lookup().catch((e) => { conn = null; throw e; }));

function post(c, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(c.base + "/mcp");
    const req = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname, method: "POST", timeout: 1000000,
      headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "Authorization": `Bearer ${c.token}`, "Content-Length": Buffer.byteLength(body) },
    }, (res) => {
      if (res.statusCode === 401) {   // locking UVMC changes the panel's key: look it up again
        res.resume();
        reject(new Error("the panel's key changed (UVMC was locked)"));
        return;
      }
      const chunks = [];
      res.on("data", (d) => chunks.push(d));
      res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8").trim()));
    });
    req.on("timeout", () => req.destroy(new Error("no answer from the UVMC machine")));
    req.on("error", reject);
    req.end(body);
  });
}

async function handle(line) {
  let id = null;
  try { id = JSON.parse(line).id ?? null; } catch { return; }
  for (const attempt of [1, 2]) {   // the machine's address can change: look it up again once
    try {
      const reply = await post(await connection(), line);
      if (reply) process.stdout.write(reply.replace(/\r?\n/g, " ") + "\n");
      return;
    } catch (e) {
      conn = null;
      log(`attempt ${attempt}: ${e.message}`);
      if (attempt === 2 && id !== null) {
        process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: {
          code: -32002, message: `Could not reach the UVMC machine: ${e.message}` } }) + "\n");
      }
    }
  }
}

readline.createInterface({ input: process.stdin, terminal: false }).on("line", (line) => {
  line = line.trim();
  if (line) handle(line);
});
