"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ChannelSort = api;
    root.sortChannelIds = api.sortChannelIds;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function isMiniChannel(channel) {
    return /-mini$/i.test(String((channel && channel.name) || "").trim());
  }

  function hasCurrentMinuteCalls(channel) {
    const cells = channel && Array.isArray(channel.cells) ? channel.cells : [];
    if (!cells.length) return false;
    const latest = cells[cells.length - 1] || {};
    const total = latest.total != null
      ? latest.total
      : (Number(latest.success || 0) + Number(latest.errors || 0));
    return Number(total) > 0;
  }

  function numericId(id) {
    const n = Number(id);
    return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
  }

  // Fixed group order:
  // 0. non-mini channels active in the current minute
  // 1. -mini channels active in the current minute
  // 2. non-mini channels idle in the current minute
  // 3. -mini channels idle in the current minute
  // Within each group, keep the user's saved drag order; otherwise use ID.
  function sortChannelIds(allIds, channels, savedOrder) {
    const saved = Array.isArray(savedOrder) ? savedOrder : [];
    const savedRank = new Map(saved.map((id, index) => [String(id), index]));
    const ids = Array.from(allIds || [], String);

    function groupOf(id) {
      const channel = (channels && channels[id]) || {};
      const active = hasCurrentMinuteCalls(channel);
      const mini = isMiniChannel(channel);
      if (active && !mini) return 0;
      if (active && mini) return 1;
      if (!active && !mini) return 2;
      return 3;
    }

    return ids.sort((a, b) => {
      const groupDiff = groupOf(a) - groupOf(b);
      if (groupDiff !== 0) return groupDiff;

      const aRank = savedRank.has(a) ? savedRank.get(a) : Number.MAX_SAFE_INTEGER;
      const bRank = savedRank.has(b) ? savedRank.get(b) : Number.MAX_SAFE_INTEGER;
      if (aRank !== bRank) return aRank - bRank;

      return numericId(a) - numericId(b) || a.localeCompare(b);
    });
  }

  return { isMiniChannel, hasCurrentMinuteCalls, sortChannelIds };
});
