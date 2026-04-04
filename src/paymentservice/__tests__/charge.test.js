const charge = require('../charge');

const validRequest = {
  amount: { currency_code: 'USD', units: 10, nanos: 0 },
  credit_card: {
    credit_card_number: '4432-8015-6152-0454',
    credit_card_cvv: 672,
    credit_card_expiration_year: 2030,
    credit_card_expiration_month: 1,
  },
};

describe('charge', () => {
  test('valid VISA card returns transaction_id', () => {
    const result = charge(validRequest);
    expect(result).toHaveProperty('transaction_id');
    expect(result.transaction_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    );
  });

  test('valid MasterCard is accepted', () => {
    const request = {
      ...validRequest,
      credit_card: {
        ...validRequest.credit_card,
        credit_card_number: '5105-1051-0510-5100',
      },
    };
    const result = charge(request);
    expect(result).toHaveProperty('transaction_id');
  });

  test('each transaction gets a unique id', () => {
    const result1 = charge(validRequest);
    const result2 = charge(validRequest);
    expect(result1.transaction_id).not.toBe(result2.transaction_id);
  });

  test('invalid card number throws InvalidCreditCard', () => {
    const request = {
      ...validRequest,
      credit_card: {
        ...validRequest.credit_card,
        credit_card_number: '0000-0000-0000-0000',
      },
    };
    expect(() => charge(request)).toThrow('Credit card info is invalid');
  });

  test('AMEX card throws UnacceptedCreditCard', () => {
    const request = {
      ...validRequest,
      credit_card: {
        ...validRequest.credit_card,
        credit_card_number: '3782-822463-10005',
      },
    };
    expect(() => charge(request)).toThrow('cannot process');
  });

  test('expired card throws ExpiredCreditCard', () => {
    const request = {
      ...validRequest,
      credit_card: {
        ...validRequest.credit_card,
        credit_card_expiration_year: 2020,
        credit_card_expiration_month: 1,
      },
    };
    expect(() => charge(request)).toThrow('expired');
  });

  test('card expiring this month is still valid', () => {
    const now = new Date();
    const request = {
      ...validRequest,
      credit_card: {
        ...validRequest.credit_card,
        credit_card_expiration_year: now.getFullYear(),
        credit_card_expiration_month: now.getMonth() + 1,
      },
    };
    const result = charge(request);
    expect(result).toHaveProperty('transaction_id');
  });
});
