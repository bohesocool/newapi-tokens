const test = require('node:test');
const assert = require('node:assert/strict');

const { sortChannelIds } = require('../app/static/js/channel_sort.js');

function channel(name, totals) {
  return { name, cells: totals.map(total => ({ total })) };
}

test('sorts active non-mini, active mini, idle non-mini, idle mini', () => {
  const channels = {
    '1': channel('普通渠道', [0, 12]),
    '2': channel('普通渠道-mini', [0, 4]),
    '3': channel('普通渠道-空闲', [0, 0]),
    '4': channel('普通渠道-mini', [7, 0]),
  };

  assert.deepEqual(
    sortChannelIds(['4', '3', '2', '1'], channels, []),
    ['1', '2', '3', '4'],
  );
});

test('uses only the newest minute and keeps saved order inside each group', () => {
  const channels = {
    '10': channel('A', [8, 0]),
    '11': channel('B', [0, 3]),
    '12': channel('C-mini', [5, 0]),
    '13': channel('D-mini', [0, 2]),
  };

  assert.deepEqual(
    sortChannelIds(['10', '11', '12', '13'], channels, ['12', '11', '10', '13']),
    ['11', '13', '10', '12'],
  );
});

test('requires the channel name to end with -mini', () => {
  const channels = {
    '20': channel('mini-preview', [1]),
    '21': channel('MINI', [1]),
  };

  assert.deepEqual(sortChannelIds(['20', '21'], channels, []), ['20', '21']);
});
