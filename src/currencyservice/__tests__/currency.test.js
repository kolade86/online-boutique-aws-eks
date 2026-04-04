const data = require('../data/currency_conversion.json');

/**
 * Extracted from server.js for testability.
 * Handles decimal/fractional carrying with nanosecond precision.
 */
function _carry(amount) {
  const fractionSize = Math.pow(10, 9);
  amount.nanos += (amount.units % 1) * fractionSize;
  amount.units = Math.floor(amount.units) + Math.floor(amount.nanos / fractionSize);
  amount.nanos = amount.nanos % fractionSize;
  return amount;
}

/**
 * Extracted from server.js for testability.
 * Converts currency via EUR as intermediate.
 */
function convert(from, toCode) {
  const euros = _carry({
    units: from.units / data[from.currency_code],
    nanos: from.nanos / data[from.currency_code],
  });
  euros.nanos = Math.round(euros.nanos);

  const result = _carry({
    units: euros.units * data[toCode],
    nanos: euros.nanos * data[toCode],
  });
  result.units = Math.floor(result.units);
  result.nanos = Math.floor(result.nanos);
  result.currency_code = toCode;
  return result;
}

describe('_carry', () => {
  test('no-op when nanos are within range', () => {
    const result = _carry({ units: 5, nanos: 500000000 });
    expect(result.units).toBe(5);
    expect(result.nanos).toBe(500000000);
  });

  test('carries nanos overflow into units', () => {
    const result = _carry({ units: 1, nanos: 1500000000 });
    expect(result.units).toBe(2);
    expect(result.nanos).toBe(500000000);
  });

  test('handles fractional units', () => {
    const result = _carry({ units: 1.5, nanos: 0 });
    expect(result.units).toBe(1);
    expect(result.nanos).toBe(500000000);
  });

  test('handles zero values', () => {
    const result = _carry({ units: 0, nanos: 0 });
    expect(result.units).toBe(0);
    expect(result.nanos).toBe(0);
  });
});

describe('convert', () => {
  test('EUR to USD conversion', () => {
    const result = convert(
      { units: 10, nanos: 0, currency_code: 'EUR' },
      'USD'
    );
    expect(result.currency_code).toBe('USD');
    expect(result.units).toBe(11);
    expect(result.nanos).toBeGreaterThanOrEqual(0);
  });

  test('USD to EUR conversion', () => {
    const result = convert(
      { units: 11, nanos: 0, currency_code: 'USD' },
      'EUR'
    );
    expect(result.currency_code).toBe('EUR');
    // 11 USD / 1.1305 ~= 9.73 EUR
    expect(result.units).toBeGreaterThanOrEqual(9);
    expect(result.units).toBeLessThanOrEqual(10);
  });

  test('same currency returns same value', () => {
    const result = convert(
      { units: 25, nanos: 0, currency_code: 'CAD' },
      'CAD'
    );
    expect(result.currency_code).toBe('CAD');
    expect(result.units).toBe(25);
    expect(result.nanos).toBe(0);
  });

  test('conversion with nanos', () => {
    const result = convert(
      { units: 5, nanos: 990000000, currency_code: 'USD' },
      'EUR'
    );
    expect(result.currency_code).toBe('EUR');
    // ~5.99 USD should be ~5.30 EUR
    expect(result.units).toBeGreaterThanOrEqual(5);
    expect(result.units).toBeLessThanOrEqual(6);
  });

  test('zero amount stays zero', () => {
    const result = convert(
      { units: 0, nanos: 0, currency_code: 'USD' },
      'EUR'
    );
    expect(result.units).toBe(0);
    expect(result.nanos).toBe(0);
  });
});

describe('currency data', () => {
  test('EUR base rate is 1.0', () => {
    expect(data['EUR']).toBe('1.0');
  });

  test('contains expected currencies', () => {
    const expected = ['USD', 'EUR', 'JPY', 'GBP', 'CAD', 'TRY'];
    for (const code of expected) {
      expect(data).toHaveProperty(code);
    }
  });

  test('all rates are positive numbers', () => {
    for (const [code, rate] of Object.entries(data)) {
      expect(parseFloat(rate)).toBeGreaterThan(0);
    }
  });
});
