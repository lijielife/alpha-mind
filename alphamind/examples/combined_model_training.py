# -*- coding: utf-8 -*-
"""
Created on 2017-11-8

@author: cheng.li
"""

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from alphamind.api import *
from PyFin.api import *

plt.style.use('ggplot')

"""
Back test parameter settings
"""

start_date = '2012-01-01'
end_date = '2017-11-06'
benchmark_code = 905
universe_name = 'zz500'
universe = Universe(universe_name, [universe_name])
frequency = '1w'
batch = 8
method = 'risk_neutral'
use_rank = 100
industry_lower = 1.
industry_upper = 1.
neutralize_risk = ['SIZE'] + industry_styles
constraint_risk = ['SIZE'] + industry_styles
size_risk_lower = 0
size_risk_upper = 0
turn_over_target_base = 0.2
horizon = map_freq(frequency)

executor = NaiveExecutor()

"""
Model phase: we need 1 constant linear model and one linear regression model
"""

const_features = ["IVR", "eps_q", "DivP", "CFinc1", "BDTO"]
const_weights = np.array([0.05, 0.2, 0.075, 0.15, 0.05])

const_model = ConstLinearModel(features=const_features,
                               weights=const_weights)

linear_model_features = {
    'eps': LAST('eps_q'),
    'roe': LAST('roe_q'),
    'bdto': LAST('BDTO'),
    'cfinc1': LAST('CFinc1'),
    'chv': LAST('CHV'),
    'ivr': LAST('IVR'),
    'val': LAST('VAL'),
    'grev': LAST('GREV')
}

"""
Data phase
"""

engine = SqlEngine()

linear_model_factor_data = fetch_data_package(engine,
                                              alpha_factors=linear_model_features,
                                              start_date=start_date,
                                              end_date=end_date,
                                              frequency=frequency,
                                              universe=universe,
                                              benchmark=benchmark_code,
                                              batch=batch,
                                              neutralized_risk=neutralize_risk,
                                              pre_process=[winsorize_normal, standardize],
                                              post_process=[winsorize_normal, standardize],
                                              warm_start=batch)

train_x = linear_model_factor_data['train']['x']
train_y = linear_model_factor_data['train']['y']
ref_dates = sorted(train_x.keys())

predict_x = linear_model_factor_data['predict']['x']
predict_y = linear_model_factor_data['predict']['y']
settlement = linear_model_factor_data['settlement']
linear_model_features = linear_model_factor_data['x_names']

const_model_factor_data = engine.fetch_data_range(universe,
                                                  const_features,
                                                  dates=ref_dates,
                                                  benchmark=benchmark_code)['factor']

"""
Training phase
"""

models_series = pd.Series()

for ref_date in ref_dates:
    x = train_x[ref_date]
    y = train_y[ref_date].flatten()

    model = LinearRegression(linear_model_features, fit_intercept=False)
    model.fit(x, y)
    models_series.loc[ref_date] = model
    alpha_logger.info('trade_date: {0} training finished'.format(ref_date))


"""
Predicting and re-balance phase
"""

factor_groups = const_model_factor_data.groupby('trade_date')

rets = []
turn_overs = []
leverags = []
previous_pos = pd.DataFrame()

for i, value in enumerate(factor_groups):
    date = value[0]
    data = value[1]
    ref_date = date.strftime('%Y-%m-%d')

    total_data = data.dropna()
    alpha_logger.info('{0}: {1}'.format(date, len(total_data)))
    risk_exp = total_data[neutralize_risk].values.astype(float)
    industry = total_data.industry_code.values
    benchmark_w = total_data.weight.values

    constraint_exp = total_data[constraint_risk].values
    risk_exp_expand = np.concatenate((constraint_exp, np.ones((len(risk_exp), 1))), axis=1).astype(float)

    risk_names = constraint_risk + ['total']
    risk_target = risk_exp_expand.T @ benchmark_w

    lbound = np.zeros(len(total_data))
    ubound = 0.015 + benchmark_w * 0.

    constraint = Constraints(risk_exp_expand, risk_names)
    for i, name in enumerate(risk_names):
        if name == 'total':
            constraint.set_constraints(name,
                                       lower_bound=risk_target[i],
                                       upper_bound=risk_target[i])
        elif name == 'SIZE':
            base_target = abs(risk_target[i])
            constraint.set_constraints(name,
                                       lower_bound=risk_target[i] + base_target * size_risk_lower,
                                       upper_bound=risk_target[i] + base_target * size_risk_upper)
        else:
            constraint.set_constraints(name,
                                       lower_bound=risk_target[i] * industry_lower,
                                       upper_bound=risk_target[i] * industry_upper)

    factor_values = factor_processing(total_data[const_features].values,
                                      pre_process=[winsorize_normal, standardize],
                                      risk_factors=risk_exp,
                                      post_process=[winsorize_normal, standardize])

    # const linear model
    er1 = const_model.predict(factor_values)

    # linear regression model
    models = models_series[models_series.index <= date]
    model = models[-1]
    x = predict_x[date]
    er2 = model.predict(x)

    # combine model
    er1_table = pd.DataFrame({'er1': er1 / er1.std(), 'code': total_data.code.values})
    er2_table = pd.DataFrame({'er2': er2 / er2.std(), 'code': settlement.loc[settlement.trade_date == date, 'code'].values})
    er_table = pd.merge(er1_table, er2_table, on=['code'], how='left').fillna(0)

    er = (er_table.er1 + er_table.er2).values

    codes = total_data['code'].values

    if previous_pos.empty:
        current_position = None
        turn_over_target = None
    else:
        previous_pos.set_index('code', inplace=True)
        remained_pos = previous_pos.loc[codes]

        remained_pos.fillna(0., inplace=True)
        turn_over_target = turn_over_target_base
        current_position = remained_pos.weight.values

    try:
        target_pos, _ = er_portfolio_analysis(er,
                                              industry,
                                              None,
                                              constraint,
                                              False,
                                              benchmark_w,
                                              method=method,
                                              use_rank=use_rank,
                                              turn_over_target=turn_over_target,
                                              current_position=current_position,
                                              lbound=lbound,
                                              ubound=ubound)
    except ValueError:
        alpha_logger.info('{0} full rebalance'.format(date))
        target_pos, _ = er_portfolio_analysis(er,
                                              industry,
                                              None,
                                              constraint,
                                              False,
                                              benchmark_w,
                                              method=method,
                                              use_rank=use_rank,
                                              lbound=lbound,
                                              ubound=ubound)

    target_pos['code'] = total_data['code'].values

    turn_over, executed_pos = executor.execute(target_pos=target_pos)

    executed_codes = executed_pos.code.tolist()
    dx_returns = engine.fetch_dx_return(date, executed_codes, horizon=horizon)

    result = pd.merge(executed_pos, total_data[['code', 'weight']], on=['code'], how='inner')
    result = pd.merge(result, dx_returns, on=['code'])

    leverage = result.weight_x.abs().sum()

    ret = (result.weight_x - result.weight_y * leverage / result.weight_y.sum()).values @ result.dx.values
    rets.append(ret)
    executor.set_current(executed_pos)
    turn_overs.append(turn_over)
    leverags.append(leverage)

    previous_pos = executed_pos
    alpha_logger.info('{0} is finished'.format(date))

ret_df = pd.DataFrame({'returns': rets, 'turn_over': turn_overs, 'leverage': leverage}, index=ref_dates)
ret_df.loc[advanceDateByCalendar('china.sse', ref_dates[-1], frequency)] = 0.
ret_df = ret_df.shift(1)
ret_df.iloc[0] = 0.
ret_df['tc_cost'] = ret_df.turn_over * 0.002

ret_df[['returns', 'tc_cost']].cumsum().plot(figsize=(12, 6),
                                             title='Fixed frequency rebalanced: {0}'.format(frequency),
                                             secondary_y='tc_cost')
plt.show()
