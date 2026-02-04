import json
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState
from typing import Any
from typing import List
import pandas as pd
import numpy as np
import math
from scipy.stats import norm

"""
Volcanic Rock Trader - Multi-Strike Options Trading Strategy

USAGE:
------
From the black_scholes.ipynb notebook, extract the optimal parameters:
    best_params = optimization_results.iloc[0]
    optimal_window = int(best_params['window'])
    optimal_std_mult = best_params['std_mult']

Then initialize the trader with these parameters:
    trader = Trader(optimal_window=optimal_window, optimal_std_mult=optimal_std_mult)

This trader will now trade on all 5 strikes [9500, 9750, 10000, 10250, 10500]
using the optimized parameters that were validated on out-of-sample data.

Default fallback parameters if not provided:
    - rolling_window: 40
    - std_multiplier: 1.0
"""


class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(
            self.to_json(
                [
                    self.compress_state(state, ""),
                    self.compress_orders(orders),
                    conversions,
                    "",
                    "",
                ]
            )
        )

        # We truncate state.traderData, trader_data, and self.logs to the same max. length to fit the log limit
        max_item_length = (self.max_log_length - base_length) // 3

        print(
            self.to_json(
                [
                    self.compress_state(state, self.truncate(state.traderData, max_item_length)),
                    self.compress_orders(orders),
                    conversions,
                    self.truncate(trader_data, max_item_length),
                    self.truncate(self.logs, max_item_length),
                ]
            )
        )

        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp,
            trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        compressed = []
        for listing in listings.values():
            compressed.append([listing.symbol, listing.product, listing.denomination])

        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [order_depth.buy_orders, order_depth.sell_orders]

        return compressed

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for trade in arr:
                compressed.append(
                    [
                        trade.symbol,
                        trade.price,
                        trade.quantity,
                        trade.buyer,
                        trade.seller,
                        trade.timestamp,
                    ]
                )

        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        for product, observation in observations.conversionObservations.items():
            conversion_observations[product] = [
                observation.bidPrice,
                observation.askPrice,
                observation.transportFees,
                observation.exportTariff,
                observation.importTariff,
                observation.sugarPrice,
                observation.sunlightIndex,
            ]

        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for order in arr:
                compressed.append([order.symbol, order.price, order.quantity])

        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        if len(value) <= max_length:
            return value

        return value[: max_length - 3] + "..."


logger = Logger()


# Black-Scholes functions
def bs_call_price(S, K, r, T, sigma):
    """Calculate Black-Scholes call option price"""
    if sigma == 0 or T == 0:
        return max(S - K, 0)
    
    d1 = (np.log(S/K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r*T) * norm.cdf(d2)


def vega(S, K, r, T, sigma):
    """Calculate vega for Newton-Raphson implied vol calculation"""
    if sigma == 0 or T == 0:
        return 0
    d1 = (np.log(S/K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * norm.pdf(d1) * math.sqrt(T)


def implied_vol(St, Vt, K, TTE, r=0.0, initial_guess=0.2, tol=1e-3, max_iter=10):
    """Calculate implied volatility using Newton-Raphson method"""
    if St == None or Vt == None or K == None:
        return float("nan")
    sigma = initial_guess
    for _ in range(max_iter):
        price = bs_call_price(St, K, r, TTE, sigma)
        diff = price - Vt
        
        if abs(diff) < tol:
            return sigma
        
        vega_val = vega(St, K, r, TTE, sigma)
        if vega_val == 0:
            return float("nan")
        sigma -= diff / vega_val
    return float("nan")


def moneyness(K, St, TTE):
    """Calculate moneyness"""
    if St == 0 or TTE == 0:
        return 0
    return np.log(K/St) / np.sqrt(TTE)


class Trader:
    def __init__(self, fitted_iv_coeffs=None, optimal_params_by_strike=None):
        # Trade all 5 strikes instead of just 10000
        self.voucher_strikes = [9500, 9750, 10000, 10250, 10500]
        self.TTE = 4  # Time to expiration
        self.r = 0.0  # Risk-free rate
        
        # Fitted IV coefficients from parabolic fit to moneyness vs IV
        # Format: [a, b, c] for IV = a*m^2 + b*m + c
        self.fitted_iv_coeffs = [3.32684505, -0.01158739, 0.00982989]  # Default fallback
        
        # Per-strike optimal parameters from walk-forward optimization
        # Format: {strike: {'window': w, 'std_multiplier': s}, ...}
        # These were optimized to maximize Sharpe ratio on out-of-sample data
        if optimal_params_by_strike is None:
            # Default fallback parameters
            optimal_params_by_strike = {
                9500: {'window': 30, 'std_multiplier': 0.5},
                9750: {'window': 30, 'std_multiplier': 0.7},
                10000: {'window': 20, 'std_multiplier': 0.5},
                10250: {'window': 10, 'std_multiplier': 1.0},
                10500: {'window': 50, 'std_multiplier': 0.3},
            }
        self.optimal_params_by_strike = optimal_params_by_strike
        
        # Max trade size per signal
        self.max_trade_size = 50
        self.max_position = 200
        self.min_position = -200
        
        # Storage for rolling calculations
        self.diff_history = {strike: [] for strike in self.voucher_strikes}
        
    def get_position(self, product: str, state: TradingState):
        try:
            return int(state.position[product])
        except KeyError:
            return 0
    
    def get_bid_ask(self, order_depth: OrderDepth):
        """Extract best bid and ask from order depth"""
        best_bid = None
        best_bid_amount = 0
        best_ask = None
        best_ask_amount = 0
        
        if len(order_depth.buy_orders) > 0:
            best_bid, best_bid_amount = list(order_depth.buy_orders.items())[0]
        if len(order_depth.sell_orders) > 0:
            best_ask, best_ask_amount = list(order_depth.sell_orders.items())[0]
            
        return best_bid, best_bid_amount, best_ask, best_ask_amount
    
    def volcanic_rock_voucher(self, strike: int, voucher_depth: OrderDepth, 
                              underlying_mid: float, position: int):
        """Trade a single volcanic rock voucher strike"""
        orders = []
        
        best_bid, best_bid_amount, best_ask, best_ask_amount = self.get_bid_ask(voucher_depth)
        
        if best_bid is None or best_ask is None:
            return orders
        
        # Get strike-specific parameters
        strike_params = self.optimal_params_by_strike.get(strike, 
                                                          {'window': 20, 'std_multiplier': 0.5})
        rolling_window = strike_params['window']
        std_multiplier = strike_params['std_multiplier']
        
        # Calculate moneyness
        m = moneyness(strike, underlying_mid, self.TTE)
        
        # Use fitted IV from parabolic curve instead of calculating from market price
        fitted_iv = np.polyval(self.fitted_iv_coeffs, m)
        
        if fitted_iv <= 0:  # Invalid IV
            return orders
        
        # Calculate theoretical price using Black-Scholes with fitted IV
        theoretical_price = bs_call_price(underlying_mid, strike, self.r, self.TTE, fitted_iv)
        
        # Calculate difference
        actual_mid = (best_bid + best_ask) / 2
        diff = theoretical_price - actual_mid

        # Store difference for rolling calculations
        self.diff_history[strike].append(diff)
        if len(self.diff_history[strike]) > rolling_window * 2:
            self.diff_history[strike].pop(0)
        
        # Calculate rolling mean and std
        if len(self.diff_history[strike]) >= rolling_window:
            rolling_diffs = np.array(self.diff_history[strike][-rolling_window:])
            rolling_mean = np.mean(rolling_diffs)
            rolling_std = np.std(rolling_diffs, ddof=1)
            
            signal_upper = rolling_mean + std_multiplier * rolling_std
            signal_lower = rolling_mean - std_multiplier * rolling_std
            
            # Generate trading signals
                # Buy signal: theoretical price significantly above market price
            if diff > signal_upper:
                # Buy at ask (cap trades to max_trade_size)
                available = abs(best_ask_amount)
                space = self.max_position - position
                buy_size = int(min(available, space if space > 0 else 0, self.max_trade_size))
                if buy_size > 0:
                    orders.append(Order(f"VOLCANIC_ROCK_VOUCHER_{strike}", best_ask, buy_size))

            elif diff < signal_lower:
                space_for_short = position - self.min_position
                
                if space_for_short > 0:
                    available_in_orderbook = abs(best_bid_amount)
                    sell_size = int(min(available_in_orderbook, space_for_short, self.max_trade_size))
                    
                    if sell_size > 0:
                        # Negative quantity for sell orders
                        orders.append(Order(f"VOLCANIC_ROCK_VOUCHER_{strike}", best_bid, -sell_size))
                    
        return orders
    
    def run(self, state: TradingState):
        result = {}
        
        # Get underlying price
        if "VOLCANIC_ROCK" not in state.order_depths:
            return result, 0, ""
        
        rock_depth = state.order_depths["VOLCANIC_ROCK"]
        rock_bid, rock_bid_amount, rock_ask, rock_ask_amount = self.get_bid_ask(rock_depth)
        
        if rock_bid is None or rock_ask is None:
            return result, 0, ""
        
        underlying_mid = (rock_bid + rock_ask) / 2
        
        # Trade each voucher strike
        for strike in self.voucher_strikes:
            product = f"VOLCANIC_ROCK_VOUCHER_{strike}"
            if product not in state.order_depths:
                continue
            
            voucher_depth = state.order_depths[product]
            position = self.get_position(product, state)
            voucher_orders = self.volcanic_rock_voucher(strike, voucher_depth, underlying_mid, position)
            result[product] = voucher_orders
        
        conversions = 0
        traderData = "x"
        logger.flush(state, result, conversions, traderData)
        
        return result, conversions, traderData


# Example usage
# trader = VolcanicRockTrader()
# trader.run(state)
