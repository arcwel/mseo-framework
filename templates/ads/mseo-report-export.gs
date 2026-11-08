/**
 * mseo report export: a Google Ads Script that writes the account's weekly report tables to one Google Sheet.
 *
 * Install once per customer (Google Ads > Tools > Bulk actions > Scripts > +, paste, authorize, schedule weekly,
 * Monday 03:00 account time). No API developer token and no OAuth client are needed: scripts run inside the
 * account. `mseo ads script install --workspace S --dry-run` prints this file with the Sheet ID filled in.
 *
 * Layout. The FIRST sheet of the spreadsheet (gid 0, the default sheet, renamed) is `_manifest`: one row per table
 * with its row count and the gid of its tab. mseo downloads gid 0 first, then each table by gid
 * (`/export?format=csv&gid=<gid>`), so the tab names never matter and no name-based query is needed.
 *
 * Every tab starts with four header lines the mseo adapter checks before it keeps anything:
 *   # mseo.ads_sheet/1
 *   customer_id,<NNN-NNN-NNNN>
 *   period,<YYYY-MM-DD>,<YYYY-MM-DD>
 *   generated,<ISO time>
 * then the column header and the rows. The customer_id comes from AdsApp itself, so the account check does not
 * depend on reading a page. Rows per table are capped at MAX_ROWS. Every cell is written as plain text
 * (setNumberFormat("@") before setValues), so dates stay ISO text and a search term starting with = + - or @ is
 * never evaluated as a formula.
 *
 * Errors. Each table is built inside its own try/catch: a failing GAQL writes the table's header with no rows and a
 * row to the `_errors` tab (table, error); the other tabs still land. `_errors` is always present, possibly empty.
 *
 * Not available. Auction insights has no GAQL view and no AdsApp report, so this script cannot write it; nothing in
 * mseo expects an auction insights table.
 *
 * APPLY. When true, the script also reads the `approved_changes` tab (the CSV `mseo actions apply` writes, pasted
 * by the operator) and applies each row: negative keywords, keyword and ad-group pauses, budgets. The script checks
 * no approval record and no item hash: the approval lives in mseo (outside the workspace), and the Sheet tab is the
 * gate, so share the Sheet with the operator only. Results go to the `applied_log` table (applied_at, batch_id, n,
 * type, target, result), written like every other table and kept across runs; a row is applied once per
 * batch#n#campaign and retried only while its last result starts with "failed:". `mseo actions status` reads the
 * log back and marks the batch items applied. Leave APPLY false until the first batch is reviewed on paper.
 */

var SHEET_ID = "__SHEET_ID__";   // filled by mseo ads script install
var MAX_ROWS = 5000;
var APPLY = false;
var SEARCH_TERMS_DAYS = 30;      // search terms need a longer window than one week to be useful
var MARKER = "# mseo.ads_sheet/1";
var HEADER_LINES = 4;            // marker, customer_id, period, generated; the column header is line 5
var MANIFEST = "_manifest";
var ERRORS = "_errors";
var APPLIED_LOG = "applied_log";
var APPROVED = "approved_changes";
var LOG_HEADER = ["applied_at", "batch_id", "n", "type", "target", "result"];

function main() {
  var period = lastCompleteIsoWeek_();
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var cid = AdsApp.currentAccount().getCustomerId();
  var builders = {
    campaigns: campaigns_,
    ad_groups: adGroups_,
    keywords: keywords_,
    search_terms: searchTerms_,
    assets: assets_,
    locations: locations_,
    ad_schedule: adSchedule_,
    change_history: changeHistory_,
    conversion_actions: conversionActions_,
    budgets: budgets_
  };
  var errors = [];
  var manifest = [];
  for (var name in builders) {
    var t;
    try {
      t = builders[name](period);
    } catch (e) {
      errors.push([name, String(e)]);
      t = { header: HEADERS[name], rows: [] };
    }
    var sheet = tab_(ss, name);
    writeTable_(sheet, cid, period, t.header, t.rows);
    manifest.push([name, t.rows.length, sheet.getSheetId()]);
  }
  var log = ss.getSheetByName(APPLIED_LOG);
  var logRows = log ? existingLog_(log) : [];
  if (APPLY) {
    try { logRows = applyApproved_(ss, logRows); }
    catch (e) { errors.push([APPLIED_LOG, String(e)]); }
  }
  log = tab_(ss, APPLIED_LOG);
  writeTable_(log, cid, period, LOG_HEADER, logRows);
  manifest.push([APPLIED_LOG, logRows.length, log.getSheetId()]);
  var err = tab_(ss, ERRORS);
  writeTable_(err, cid, period, ["table", "error"], errors);
  manifest.push([ERRORS, errors.length, err.getSheetId()]);
  var m = manifestTab_(ss);
  writeTable_(m, cid, period, ["table", "rows", "gid"], manifest);
}

// ---------------------------------------------------------------- period

function lastCompleteIsoWeek_() {
  var tz = AdsApp.currentAccount().getTimeZone();
  var today = new Date(Utilities.formatDate(new Date(), tz, "yyyy-MM-dd") + "T00:00:00Z");
  var dow = (today.getUTCDay() + 6) % 7;                 // Monday = 0
  var lastMonday = new Date(today.getTime() - (dow + 7) * 86400000);
  var lastSunday = new Date(lastMonday.getTime() + 6 * 86400000);
  return { start: iso_(lastMonday), end: iso_(lastSunday), tz: tz };
}

function iso_(d) { return Utilities.formatDate(d, "UTC", "yyyy-MM-dd"); }
function gaql_(d) { return d.replace(/-/g, ""); }

// ---------------------------------------------------------------- tables (GAQL through AdsApp.report)

// Column headers, declared once so a failing query still writes a well-formed empty table.
var HEADERS = {
  campaigns: ["campaign.name", "campaign.advertising_channel_type", "campaign.status", "campaign.bidding_strategy_type",
    "campaign_budget.amount_micros", "metrics.cost_micros", "metrics.clicks", "metrics.impressions", "metrics.conversions",
    "metrics.conversions_value", "metrics.phone_calls", "metrics.search_impression_share",
    "metrics.search_budget_lost_impression_share", "metrics.search_rank_lost_impression_share"],
  ad_groups: ["campaign.name", "ad_group.name", "ad_group.status", "metrics.cost_micros", "metrics.clicks", "metrics.impressions",
    "metrics.conversions"],
  keywords: ["campaign.name", "ad_group.name", "ad_group_criterion.keyword.text", "ad_group_criterion.keyword.match_type",
    "ad_group_criterion.status", "ad_group_criterion.quality_info.quality_score", "metrics.cost_micros", "metrics.clicks",
    "metrics.impressions", "metrics.conversions", "metrics.ctr", "metrics.average_cpc"],
  search_terms: ["campaign.name", "ad_group.name", "search_term_view.search_term", "search_term_view.status",
    "segments.search_term_match_type", "metrics.cost_micros", "metrics.clicks", "metrics.impressions", "metrics.conversions"],
  assets: ["campaign.name", "ad_group.name", "ad_group_ad_asset_view.field_type", "asset.text_asset.text",
    "ad_group_ad_asset_view.performance_label", "ad_group_ad_asset_view.pinned_field", "metrics.impressions"],
  locations: ["campaign.name", "geographic_view.country_criterion_id", "geographic_view.location_type", "segments.geo_target_city",
    "segments.geo_target_state", "city_name", "state_name", "metrics.cost_micros", "metrics.clicks", "metrics.impressions",
    "metrics.conversions"],
  ad_schedule: ["campaign.name", "segments.day_of_week", "segments.hour", "metrics.cost_micros", "metrics.clicks",
    "metrics.impressions", "metrics.conversions"],
  change_history: ["change_event.change_date_time", "change_event.user_email", "change_event.change_resource_type",
    "change_event.resource_change_operation", "change_event.client_type", "campaign.name"],
  conversion_actions: ["conversion_action.name", "conversion_action.type", "conversion_action.category", "conversion_action.status",
    "conversion_action.primary_for_goal", "conversion_action.counting_type", "conversion_action.origin",
    "conversion_action.include_in_conversions_metric"],
  budgets: ["campaign.name", "campaign_budget.amount_micros", "campaign_budget.delivery_method", "campaign_budget.explicitly_shared"]
};

function rows_(query, cols) {
  var it = AdsApp.report(query).rows();
  var out = [];
  while (it.hasNext() && out.length < MAX_ROWS) {
    var r = it.next();
    out.push(cols.map(function (c) { return r[c]; }));
  }
  return { header: cols, rows: out };
}

function between_(p) { return " segments.date BETWEEN '" + p.start + "' AND '" + p.end + "' "; }

function campaigns_(p) {
  return rows_("SELECT campaign.name, campaign.advertising_channel_type, campaign.status, campaign.bidding_strategy_type, " +
    "campaign_budget.amount_micros, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, " +
    "metrics.conversions_value, metrics.phone_calls, metrics.search_impression_share, metrics.search_budget_lost_impression_share, " +
    "metrics.search_rank_lost_impression_share FROM campaign WHERE" + between_(p) + "AND campaign.status != 'REMOVED'",
    HEADERS.campaigns);
}

function adGroups_(p) {
  return rows_("SELECT campaign.name, ad_group.name, ad_group.status, metrics.cost_micros, metrics.clicks, metrics.impressions, " +
    "metrics.conversions FROM ad_group WHERE" + between_(p) + "AND ad_group.status != 'REMOVED'", HEADERS.ad_groups);
}

function keywords_(p) {
  return rows_("SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, " +
    "ad_group_criterion.status, ad_group_criterion.quality_info.quality_score, metrics.cost_micros, metrics.clicks, " +
    "metrics.impressions, metrics.conversions, metrics.ctr, metrics.average_cpc FROM keyword_view WHERE" + between_(p) +
    "AND ad_group_criterion.status != 'REMOVED'", HEADERS.keywords);
}

function searchTerms_(p) {
  var end = new Date(p.end + "T00:00:00Z");
  var start = iso_(new Date(end.getTime() - (SEARCH_TERMS_DAYS - 1) * 86400000));
  return rows_("SELECT campaign.name, ad_group.name, search_term_view.search_term, search_term_view.status, " +
    "segments.search_term_match_type, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions " +
    "FROM search_term_view WHERE segments.date BETWEEN '" + start + "' AND '" + p.end + "'", HEADERS.search_terms);
}

function assets_(p) {
  return rows_("SELECT campaign.name, ad_group.name, ad_group_ad_asset_view.field_type, asset.text_asset.text, " +
    "ad_group_ad_asset_view.performance_label, ad_group_ad_asset_view.pinned_field, metrics.impressions " +
    "FROM ad_group_ad_asset_view WHERE" + between_(p), HEADERS.assets);
}

function locations_(p) {
  // geo segments come back as resource names (geoTargetConstants/<id>); the names are resolved in a second query
  var raw = rows_("SELECT campaign.name, geographic_view.country_criterion_id, geographic_view.location_type, " +
    "segments.geo_target_city, segments.geo_target_state, metrics.cost_micros, metrics.clicks, metrics.impressions, " +
    "metrics.conversions FROM geographic_view WHERE" + between_(p),
    ["campaign.name", "geographic_view.country_criterion_id", "geographic_view.location_type", "segments.geo_target_city",
     "segments.geo_target_state", "metrics.cost_micros", "metrics.clicks", "metrics.impressions", "metrics.conversions"]);
  var names = geoNames_(raw.rows.map(function (r) { return r[3]; }).concat(raw.rows.map(function (r) { return r[4]; })));
  return {
    header: HEADERS.locations,
    rows: raw.rows.map(function (r) {
      return [r[0], r[1], r[2], r[3], r[4], names[r[3]] || "", names[r[4]] || "", r[5], r[6], r[7], r[8]];
    })
  };
}

function geoNames_(resources) {
  var unique = {};
  resources.forEach(function (x) { if (x) { unique[x] = true; } });
  var ids = Object.keys(unique);
  var out = {};
  for (var i = 0; i < ids.length; i += 200) {
    var chunk = ids.slice(i, i + 200).map(function (x) { return "'" + x + "'"; }).join(",");
    var it = AdsApp.report("SELECT geo_target_constant.resource_name, geo_target_constant.name FROM geo_target_constant " +
      "WHERE geo_target_constant.resource_name IN (" + chunk + ")").rows();
    while (it.hasNext()) {
      var r = it.next();
      out[r["geo_target_constant.resource_name"]] = r["geo_target_constant.name"];
    }
  }
  return out;
}

function adSchedule_(p) {
  return rows_("SELECT campaign.name, segments.day_of_week, segments.hour, metrics.cost_micros, metrics.clicks, " +
    "metrics.impressions, metrics.conversions FROM campaign WHERE" + between_(p), HEADERS.ad_schedule);
}

// campaign id -> name, for resources of the form customers/<cid>/campaigns/<id> (change_event has no campaign.name)
function campaignNames_() {
  var it = AdsApp.report("SELECT campaign.id, campaign.name FROM campaign").rows();
  var out = {};
  while (it.hasNext()) {
    var r = it.next();
    out[String(r["campaign.id"])] = r["campaign.name"];
  }
  return out;
}

function campaignFromResource_(resource, names) {
  var m = /\/campaigns\/(\d+)$/.exec(resource || "");
  return m && names[m[1]] ? names[m[1]] : (resource || "");
}

function changeHistory_(p) {
  var since = p.start + " 00:00:00";
  var until = p.end + " 23:59:59";
  var raw = rows_("SELECT change_event.change_date_time, change_event.user_email, change_event.change_resource_type, " +
    "change_event.resource_change_operation, change_event.client_type, change_event.campaign FROM change_event " +
    "WHERE change_event.change_date_time BETWEEN '" + since + "' AND '" + until + "' ORDER BY change_event.change_date_time LIMIT 2000",
    ["change_event.change_date_time", "change_event.user_email", "change_event.change_resource_type",
     "change_event.resource_change_operation", "change_event.client_type", "change_event.campaign"]);
  var names = campaignNames_();
  return {
    header: HEADERS.change_history,
    rows: raw.rows.map(function (r) { return [r[0], r[1], r[2], r[3], r[4], campaignFromResource_(r[5], names)]; })
  };
}

function conversionActions_() {
  return rows_("SELECT conversion_action.name, conversion_action.type, conversion_action.category, conversion_action.status, " +
    "conversion_action.primary_for_goal, conversion_action.counting_type, conversion_action.origin, " +
    "conversion_action.include_in_conversions_metric FROM conversion_action WHERE conversion_action.status != 'REMOVED'",
    HEADERS.conversion_actions);
}

function budgets_() {
  return rows_("SELECT campaign.name, campaign_budget.amount_micros, campaign_budget.delivery_method, " +
    "campaign_budget.explicitly_shared FROM campaign WHERE campaign.status = 'ENABLED'", HEADERS.budgets);
}

// ---------------------------------------------------------------- sheet writing

function tab_(ss, name) {
  var s = ss.getSheetByName(name);
  if (!s) { s = ss.insertSheet(name); }
  s.clearContents();
  return s;
}

// The manifest is the FIRST sheet (gid 0) so mseo can download it without knowing any gid: the spreadsheet's
// default sheet is renamed on the first run. A `_manifest` tab that is not first (an older install) is dropped.
function manifestTab_(ss) {
  var first = ss.getSheets()[0];
  if (first.getName() !== MANIFEST) {
    var stray = ss.getSheetByName(MANIFEST);
    if (stray) { ss.deleteSheet(stray); }
    first.setName(MANIFEST);
  }
  first.clearContents();
  return first;
}

function writeTable_(sheet, cid, period, header, rows) {
  var head = [[MARKER], ["customer_id", cid], ["period", period.start, period.end],
              ["generated", Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'")]];
  var width = Math.max(header.length, 3);
  var pad = function (r) { while (r.length < width) { r.push(""); } return r; };
  var all = head.map(pad).concat([pad(header.slice())]).concat(rows.map(function (r) { return pad(r.slice()); }));
  var range = sheet.getRange(1, 1, all.length, width);
  range.setNumberFormat("@");   // plain text: ISO dates stay text, = + - @ cells are never formulas
  range.setValues(all);
}

// ---------------------------------------------------------------- applying an approved batch (APPLY = true only)

// The data rows of the applied_log tab as it was left: after the four header lines and the column header when the
// tab has the marker, after the one header row in the layout earlier builds wrote.
function existingLog_(log) {
  var values = log.getDataRange().getValues();
  if (!values.length) { return []; }
  var skip = String(values[0][0]) === MARKER ? HEADER_LINES + 1 : 1;
  return values.slice(skip).filter(function (r) { return r[1] !== "" && r[1] !== null; })
    .map(function (r) { return LOG_HEADER.map(function (_, i) { return r[i] === undefined ? "" : r[i]; }); });
}

// mseo prefixes a CSV cell that starts with = + - or @ with a single quote; Sheets usually eats it on paste, this
// does when it did not.
function unquote_(v) {
  var s = v === null || v === undefined ? "" : String(v);
  return s.charAt(0) === "'" ? s.slice(1) : s;
}

function applyApproved_(ss, logRows) {
  var tab = ss.getSheetByName(APPROVED);
  if (!tab) { return logRows; }
  var values = tab.getDataRange().getValues();
  var last = {};   // batch#n#campaign -> last result; retried only while it starts with "failed:"
  logRows.forEach(function (r) { last[String(r[1]) + "#" + String(r[2]) + "#" + campaignOf_(String(r[4]))] = String(r[5]); });
  // columns: batch_id, n, item_hash, type, campaign, ad_group, text, match_type, value
  for (var i = 1; i < values.length; i++) {
    var r = values[i].map(unquote_);
    if (!r[0]) { continue; }
    var key = r[0] + "#" + r[1] + "#" + r[4];
    if (last[key] !== undefined && last[key].indexOf("failed:") !== 0) { continue; }
    var result;
    try { result = applyOne_(r[3], r[4], r[5], r[6], r[7], r[8]); }
    catch (e) { result = "failed: " + e; }
    // target is JSON so a campaign name holding " / " or quotes round-trips
    var row = [Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'"), r[0], r[1], r[3],
               JSON.stringify({campaign: r[4], ad_group: r[5], text: r[6]}), result];
    logRows.push(row);
    last[key] = result;
  }
  return logRows;
}

function campaignOf_(target) {
  try { var t = JSON.parse(target); if (t && typeof t === "object") { return String(t.campaign || ""); } } catch (e) {}
  return String(target).split(" / ")[0];  // rows written by an older script build
}

// GAQL string literal: the backslash first, then the quote, or a name ending in \ would unbalance the quote.
function q_(s) { return "'" + String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'"; }

// Exactly one entity, or a throw: a lookup that matches two would act on the wrong one.
function one_(it, what) {
  if (!it.hasNext()) { throw "no " + what; }
  var x = it.next();
  if (it.hasNext()) { throw "more than one " + what + "; nothing changed"; }
  return x;
}

function campaign_(name) {
  return one_(AdsApp.campaigns().withCondition("campaign.name = " + q_(name)).get(), "campaign named " + name);
}

function applyOne_(type, campaign, adGroup, text, match, value) {
  var c = campaign_(campaign);
  if (type === "negative_keyword") {
    var neg = match === "exact" ? "[" + text + "]" : match === "phrase" ? '"' + text + '"' : text;
    c.createNegativeKeyword(neg);
    return "ok: negative " + neg;
  }
  if (type === "budget") {
    var b = c.getBudget();
    if (b.isExplicitlyShared()) { return "failed: budget is shared with other campaigns; not raised"; }
    b.setAmount(Number(value));
    return "ok: budget " + value;
  }
  if (type === "pause_keyword") {
    var it = c.keywords().withCondition("ad_group.name = " + q_(adGroup))
      .withCondition("ad_group_criterion.keyword.text = " + q_(text)).get();
    var wanted = String(match || "").toUpperCase();
    var hits = [];
    while (it.hasNext()) {
      var k = it.next();
      if (!wanted || String(k.getMatchType()).toUpperCase() === wanted) { hits.push(k); }
    }
    if (hits.length !== 1) { throw (hits.length ? "more than one" : "no") + " keyword " + text + " (" + wanted + ") in " + adGroup; }
    hits[0].pause();
    return "ok: paused keyword " + text + " (" + wanted + ") in " + adGroup;
  }
  if (type === "pause_ad_group") {
    one_(c.adGroups().withCondition("ad_group.name = " + q_(adGroup)).get(), "ad group named " + adGroup).pause();
    return "ok: paused ad group " + adGroup;
  }
  return "skipped: type " + type + " is not applied by the script";
}
