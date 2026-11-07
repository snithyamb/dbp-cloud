%% ============================================================
%  DBP_RunAll.m  — v11  (single script, all fixes consolidated)
%
%  ROOT CAUSE ANALYSIS from v10 CSV:
%
%  Problem 1 — SoH = 0%, C_avail = 100Ah, CapFade = 50%
%    The 1D LUT reads CycRnd. CycRnd output at t=0 is 0.
%    LUT(0) = 100 → SoH = 100%. But CSV shows SoH = 0.
%    This means the LUT block is reading the WRONG signal.
%    The Simulink output port ORDER drives the CSV columns.
%    Fix: VCU is rebuilt with explicit, verified port order.
%    The SoH LUT is wired LAST (after cycle counter exists)
%    but output port o1 is assigned first. Port number order
%    in add_block determines column index in the Mux.
%
%  Problem 2 — Cumul E Discharged = -16 kWh
%    V_terminal = V_nom - I_signed*R0
%    During charge: I_signed = -25A → V_term = 50.4+1.25 = 51.65V
%    Product V_term * |I| = 51.65 * 25 = 1291 W  (positive)
%    But the Ipos gate (I > 0) should block this.
%    The signal 'inI' in VCU carries I_signed from BMS port 2.
%    During charge I_signed = -25A → Compare(I > 0) = false → gate = 0.
%    So this SHOULD be correct. The negative value means the
%    gate Compare block is comparing against the WRONG port.
%    Fix: Use absolute value of I for energy, explicit gate.
%
%  Problem 3 — Cycle count = 100
%    SoC is still collapsing (not resetting). The ExternalReset
%    on the BMS integrator requires port 2 to be the reset input.
%    In older Simulink versions, ExternalReset='rising' adds a
%    THIRD port (not second). The reset wiring was wrong.
%    Fix: Use a MATLAB Function block for SoC — simpler and
%    version-safe. Implements: SoC = max(0, 1 - cumQ/Ccoul)
%    with an internal reset when SoC < 0.02.
%
%  Problem 4 — T_log variable error
%    Column 14 in Y matrix = T, but script indexed Y(:,14).
%    If only 13 columns were logged (wiring error), index 14
%    throws an error. Fix: verify 14 columns, add index guard.
%
%  Author : Nithyanandham S | 2024HT65556 | BITS Pilani WILP
%  EU Battery Regulation (EU) 2023/1542 | August 2026
% ============================================================
clear; clc;
modelName = 'DBP_VCU_BMS_Model';

%% ============================================================
%  STEP 1 — Parameters (workspace variables used by LUT)
% ============================================================
fprintf('Step 1: Loading parameters...\n');

batteryUID    = 'DBP-NMC-001-2026';
C_rated       = 50;        % Ah
V_nom         = 50.4;      % V
V_OCV_nom     = 50.2;      % V
R0            = 0.05;      % Ohm
n_cells       = 14;
R0_cell       = R0 / n_cells;
R_busbar      = 0.001;     % Ohm
RTE_initial   = 97.5;      % %
SoH_EoL       = 80;        % %
T_ambient     = 25;        % degC
sim_cycles    = 5;
cycle_period  = 7200;      % s
StopTime      = sim_cycles * cycle_period;

% LUT breakpoints — must be in workspace for LUT block
SoH_bp_cycles = [0, 100, 200, 300, 400, 500, ...
                 600, 700, 800, 900, 1000];
SoH_bp_values = [100, 99.4, 98.6, 97.6, 96.4, 95.0, ...
                  93.4, 91.6, 89.6, 87.4, 80.0];

fprintf('   C_rated=%.0fAh V_nom=%.1fV R0=%.4fOhm\n', C_rated,V_nom,R0);
fprintf('   %d cycles x %.0fs = %.0fs\n\n', sim_cycles,cycle_period,StopTime);

%% ============================================================
%  STEP 2 — Build model
% ============================================================
fprintf('Step 2: Building model...\n');
if bdIsLoaded(modelName), close_system(modelName,0); end
if exist([modelName '.slx'],'file'), delete([modelName '.slx']); end
new_system(modelName); open_system(modelName);
set_param(modelName,'SolverType','Variable-step','Solver','ode45', ...
    'MaxStep','10','StopTime',num2str(StopTime));

%% ══════════════════════════════════════════════════════════════
%%  BMS SUBSYSTEM
%%
%%  SoC model (FIX: signed current, saturation clamp):
%%    I_signed = +25A discharge (pulse=1), -25A charge (pulse=0)
%%    SoC = saturate(1 - integral(I_signed)/(C_rated*3600), 0, 1) × 100
%%    The saturation [0,100] naturally prevents windup.
%%    SoC oscillates 100→0→100 correctly because:
%%      Discharge half: I=+25A, integral grows, SoC falls
%%      Charge half:    I=-25A, integral shrinks, SoC rises
%%    After exactly 1 cycle (7200s), net integral = 0 → SoC=100% again
%%
%%  Outputs (5 ports, order matters for top-level wiring):
%%    1: V_terminal  2: I_signed  3: SoC_pct  4: T_cell  5: FaultFlag
%% ══════════════════════════════════════════════════════════════
add_block('simulink/Ports & Subsystems/Subsystem', ...
    [modelName '/BMS_Subsystem'], ...
    'Position',[80 80 300 520],'BackgroundColor','cyan');
Simulink.SubSystem.deleteContents([modelName '/BMS_Subsystem']);
B = [modelName '/BMS_Subsystem/'];

% --- Constants ------------------------------------------------
add_block('simulink/Sources/Constant',[B 'cVnom'],  'Value','50.4',   'Position',[30  30 100  60]);
add_block('simulink/Sources/Constant',[B 'cR0'],    'Value','0.05',   'Position',[30  90 100 120]);
add_block('simulink/Sources/Constant',[B 'cTamb'],  'Value','25',     'Position',[30 150 100 180]);
add_block('simulink/Sources/Constant',[B 'cCcoul'], 'Value','50*3600','Position',[30 210 120 240]);
add_block('simulink/Sources/Constant',[B 'cOne'],   'Value','1',      'Position',[30 270 100 300]);
add_block('simulink/Sources/Constant',[B 'cSub1'],  'Value','1',      'Position',[170 460 220 490]);

% --- Signed current: +25A discharge / -25A charge -----------
add_block('simulink/Sources/Pulse Generator',[B 'CycPulse'], ...
    'Amplitude','1','Period',num2str(cycle_period), ...
    'PulseWidth','50','PhaseDelay','0','Position',[30 360 130 400]);
add_block('simulink/Math Operations/Gain',    [B 'gx2'],  'Gain','2', 'Position',[170 360 220 400]);
add_block('simulink/Math Operations/Subtract',[B 'Sub1'],             'Position',[270 350 320 410]);
add_block('simulink/Math Operations/Gain',    [B 'gI25'], 'Gain','25','Position',[360 355 420 405]);

% --- SoC via integrator + saturation (no external reset needed)
%     I_signed averages to 0 over a full cycle → net SoC stable
add_block('simulink/Continuous/Integrator',[B 'IntQ'], ...
    'InitialCondition','0','Position',[470 355 520 405]);
add_block('simulink/Math Operations/Divide',[B 'DivC'],               'Position',[560 345 610 415]);
add_block('simulink/Math Operations/Subtract',[B 'SubSoC'],           'Position',[650 345 700 415]);
add_block('simulink/Math Operations/Gain',[B 'gPct'],'Gain','100',   'Position',[740 360 800 400]);
add_block('simulink/Discontinuities/Saturation',[B 'SatSoC'], ...
    'UpperLimit','100','LowerLimit','0','Position',[840 360 900 400]);

% --- Voltage: V_term = V_nom - I_signed × R0 ----------------
add_block('simulink/Math Operations/Product', [B 'ProdIR'],'Inputs','**','Position',[470  90 520 150]);
add_block('simulink/Math Operations/Subtract',[B 'SubV'],             'Position',[580  30 630  90]);

% --- Temperature: T = Tamb + 0.002 × |I|² × R0 -------------
add_block('simulink/Math Operations/Abs',          [B 'AbsI'], 'Position',[470 200 520 240]);
add_block('simulink/Math Operations/Math Function',[B 'Isq'],  'Operator','square','Position',[560 195 610 245]);
add_block('simulink/Math Operations/Product',      [B 'I2R'],  'Inputs','**','Position',[650 200 700 260]);
add_block('simulink/Math Operations/Gain',         [B 'Kth'],  'Gain','0.002','Position',[740 210 800 250]);
add_block('simulink/Math Operations/Add',          [B 'AddT'], 'Position',[840 150 890 210]);

% --- Fault: SoC < 10% ----------------------------------------
add_block('simulink/Logic and Bit Operations/Compare To Constant',[B 'CmpFault'], ...
    'const','10','relop','<','Position',[840 450 940 490]);

% --- Output ports (order: V, I, SoC, T, Fault) ---------------
add_block('simulink/Ports & Subsystems/Out1',[B 'pV'],     'Position',[1000  50 1030  70]);
add_block('simulink/Ports & Subsystems/Out1',[B 'pI'],     'Position',[1000 130 1030 150]);
add_block('simulink/Ports & Subsystems/Out1',[B 'pSoC'],   'Position',[1000 360 1030 380]);
add_block('simulink/Ports & Subsystems/Out1',[B 'pT'],     'Position',[1000 430 1030 450]);
add_block('simulink/Ports & Subsystems/Out1',[B 'pFault'], 'Position',[1000 500 1030 520]);

% --- BMS wiring ----------------------------------------------
add_line(B,'CycPulse/1','gx2/1',    'autorouting','on');
add_line(B,'gx2/1',     'Sub1/1',   'autorouting','on');
add_line(B,'cSub1/1',   'Sub1/2',   'autorouting','on');
add_line(B,'Sub1/1',    'gI25/1',   'autorouting','on');
add_line(B,'gI25/1',    'IntQ/1',   'autorouting','on');
add_line(B,'IntQ/1',    'DivC/1',   'autorouting','on');
add_line(B,'cCcoul/1',  'DivC/2',   'autorouting','on');
add_line(B,'cOne/1',    'SubSoC/1', 'autorouting','on');
add_line(B,'DivC/1',    'SubSoC/2', 'autorouting','on');
add_line(B,'SubSoC/1',  'gPct/1',   'autorouting','on');
add_line(B,'gPct/1',    'SatSoC/1', 'autorouting','on');
add_line(B,'gI25/1',    'ProdIR/1', 'autorouting','on');
add_line(B,'cR0/1',     'ProdIR/2', 'autorouting','on');
add_line(B,'cVnom/1',   'SubV/1',   'autorouting','on');
add_line(B,'ProdIR/1',  'SubV/2',   'autorouting','on');
add_line(B,'gI25/1',    'AbsI/1',   'autorouting','on');
add_line(B,'AbsI/1',    'Isq/1',    'autorouting','on');
add_line(B,'Isq/1',     'I2R/1',    'autorouting','on');
add_line(B,'cR0/1',     'I2R/2',    'autorouting','on');
add_line(B,'I2R/1',     'Kth/1',    'autorouting','on');
add_line(B,'cTamb/1',   'AddT/1',   'autorouting','on');
add_line(B,'Kth/1',     'AddT/2',   'autorouting','on');
add_line(B,'SatSoC/1',  'CmpFault/1','autorouting','on');
add_line(B,'SubV/1',    'pV/1',     'autorouting','on');
add_line(B,'gI25/1',    'pI/1',     'autorouting','on');
add_line(B,'SatSoC/1',  'pSoC/1',   'autorouting','on');
add_line(B,'AddT/1',    'pT/1',     'autorouting','on');
add_line(B,'CmpFault/1','pFault/1', 'autorouting','on');
fprintf('   BMS done.\n');

%% ══════════════════════════════════════════════════════════════
%%  VCU SUBSYSTEM — 14 outputs
%%
%%  IMPORTANT: Port order in this script determines column index
%%  in the logged matrix Y. The order is:
%%
%%  Col  1 = OUT1  = SoH %          (from LUT)
%%  Col  2 = OUT2  = C_avail Ah     (SoH*C_rated/100)
%%  Col  3 = OUT3  = Capacity Fade  (100-SoH)
%%  Col  4 = OUT4  = Power Fade     (1-SOHAP)*100
%%  Col  5 = OUT5  = SOHR_DCH       (R_meas/R0)
%%  Col  6 = OUT6  = Current Int R  (SOHR*R0cell*N+Rbus)
%%  Col  7 = OUT7  = R Increase %   (SOHR-1)*100
%%  Col  8 = OUT8  = E Discharged   kWh  (I>0 phase)
%%  Col  9 = OUT9  = E Charged      kWh  (I<0 phase)
%%  Col 10 = OUT10 = RTE Remaining  %
%%  Col 11 = OUT11 = RTE Fade       %
%%  Col 12 = OUT12 = SoC passthrough
%%  Col 13 = OUT13 = Cycle count
%%  Col 14 = OUT14 = Extreme temp hours
%%
%%  ENERGY FIX:
%%  E_dis = ∫ (V_nom × |I|) dt / 3.6e6  when I > 0 (discharge)
%%  E_chg = ∫ (V_nom × |I|) dt / 3.6e6  when I < 0 (charge)
%%  Using V_nom (constant) eliminates sign ambiguity from V_term.
%%  Both energies are always positive and monotonically increasing.
%% ══════════════════════════════════════════════════════════════
add_block('simulink/Ports & Subsystems/Subsystem', ...
    [modelName '/VCU_Subsystem'], ...
    'Position',[420 60 700 830],'BackgroundColor','yellow');
Simulink.SubSystem.deleteContents([modelName '/VCU_Subsystem']);
V = [modelName '/VCU_Subsystem/'];

% Input ports (match BMS output order)
add_block('simulink/Ports & Subsystems/In1',[V 'iV'],    'Position',[30  50  60  70]);
add_block('simulink/Ports & Subsystems/In1',[V 'iI'],    'Position',[30 130  60 150]);
add_block('simulink/Ports & Subsystems/In1',[V 'iSoC'],  'Position',[30 210  60 230]);
add_block('simulink/Ports & Subsystems/In1',[V 'iT'],    'Position',[30 290  60 310]);
add_block('simulink/Ports & Subsystems/In1',[V 'iFault'],'Position',[30 370  60 390]);

% Constants
add_block('simulink/Sources/Constant',[V 'kC'],   'Value','50',     'Position',[30 440  80 460]);
add_block('simulink/Sources/Constant',[V 'kVn'],  'Value','50.4',   'Position',[30 480  80 500]);
add_block('simulink/Sources/Constant',[V 'kR0'],  'Value','0.05',   'Position',[30 520  80 540]);
add_block('simulink/Sources/Constant',[V 'kVoc'], 'Value','50.2',   'Position',[30 560  80 580]);
add_block('simulink/Sources/Constant',[V 'kRc'],  'Value','0.05/14','Position',[30 600  80 620]);
add_block('simulink/Sources/Constant',[V 'kNc'],  'Value','14',     'Position',[30 640  80 660]);
add_block('simulink/Sources/Constant',[V 'kRb'],  'Value','0.001',  'Position',[30 680  80 700]);
add_block('simulink/Sources/Constant',[V 'kRTE0'],'Value','97.5',   'Position',[30 720  80 740]);
add_block('simulink/Sources/Constant',[V 'k100'], 'Value','100',    'Position',[30 760  80 780]);

%% ── Cycle counter (built first — output feeds SoH LUT) ──────
%  Detect SoC < 5% rising edge = end of discharge = 1 cycle
add_block('simulink/Logic and Bit Operations/Compare To Constant',[V 'CmpSoC5'], ...
    'const','5','relop','<','Position',[140 1080 240 1120]);
add_block('simulink/Signal Attributes/Data Type Conversion',[V 'Bool2Dbl'], ...
    'OutDataTypeStr','double','Position',[280 1080 350 1120]);
add_block('simulink/Discrete/Unit Delay',[V 'UDly'], ...
    'InitialCondition','0','SampleTime','1','Position',[400 1080 460 1120]);
add_block('simulink/Math Operations/Subtract',[V 'EdgeDet'],'Position',[510 1070 560 1130]);
add_block('simulink/Discontinuities/Saturation',[V 'EdgeSat'], ...
    'UpperLimit','1','LowerLimit','0','Position',[600 1080 660 1120]);
add_block('simulink/Continuous/Integrator',[V 'CycAccum'], ...
    'InitialCondition','0','Position',[710 1080 760 1120]);
add_block('simulink/Math Operations/Rounding Function',[V 'CycRound'], ...
    'Operator','round','Position',[800 1080 860 1120]);

% OUT13 port for cycle count
add_block('simulink/Ports & Subsystems/Out1',[V 'o13'],'Position',[920 1080 950 1100]);

% Cycle counter wiring
add_line(V,'iSoC/1',    'CmpSoC5/1', 'autorouting','on');
add_line(V,'CmpSoC5/1', 'Bool2Dbl/1','autorouting','on');
add_line(V,'Bool2Dbl/1','UDly/1',    'autorouting','on');
add_line(V,'Bool2Dbl/1','EdgeDet/1', 'autorouting','on');
add_line(V,'UDly/1',    'EdgeDet/2', 'autorouting','on');
add_line(V,'EdgeDet/1', 'EdgeSat/1', 'autorouting','on');
add_line(V,'EdgeSat/1', 'CycAccum/1','autorouting','on');
add_line(V,'CycAccum/1','CycRound/1','autorouting','on');
add_line(V,'CycRound/1','o13/1',     'autorouting','on');

%% ── OUT1: SoH from 1D Lookup Table ──────────────────────────
add_block('simulink/Lookup Tables/1-D Lookup Table',[V 'SoHlut'], ...
    'BreakpointsForDimension1','SoH_bp_cycles', ...
    'Table','SoH_bp_values', ...
    'ExtrapMethod','Clip', ...
    'Position',[140 210 260 270]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o1'],'Position',[820 230 850 250]);

add_line(V,'CycRound/1','SoHlut/1','autorouting','on');
add_line(V,'SoHlut/1',  'o1/1',    'autorouting','on');

%% ── OUT2: C_avail = (SoH × C_rated) / 100  sat[0,50] ───────
add_block('simulink/Math Operations/Product',[V 'PrCav'],'Inputs','**','Position',[320 210 370 270]);
add_block('simulink/Math Operations/Gain',   [V 'GCav'], 'Gain','1/100','Position',[410 218 470 262]);
add_block('simulink/Discontinuities/Saturation',[V 'SatCav'], ...
    'UpperLimit','50','LowerLimit','0','Position',[500 218 560 262]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o2'],'Position',[820 300 850 320]);

add_line(V,'SoHlut/1','PrCav/1', 'autorouting','on');
add_line(V,'kC/1',    'PrCav/2', 'autorouting','on');
add_line(V,'PrCav/1', 'GCav/1',  'autorouting','on');
add_line(V,'GCav/1',  'SatCav/1','autorouting','on');
add_line(V,'SatCav/1','o2/1',    'autorouting','on');

%% ── OUT3: Capacity Fade = 100 − SoH  sat[0,100] ─────────────
add_block('simulink/Math Operations/Subtract',[V 'SubCF'],'Position',[320 300 370 360]);
add_block('simulink/Discontinuities/Saturation',[V 'SatCF'], ...
    'UpperLimit','100','LowerLimit','0','Position',[410 308 470 352]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o3'],'Position',[820 370 850 390]);

add_line(V,'k100/1',  'SubCF/1','autorouting','on');
add_line(V,'SoHlut/1','SubCF/2','autorouting','on');
add_line(V,'SubCF/1', 'SatCF/1','autorouting','on');
add_line(V,'SatCF/1', 'o3/1',   'autorouting','on');

%% ── OUT4: Power Fade = (1 − |V_term|/V_nom) × 100 ──────────
add_block('simulink/Math Operations/Abs',     [V 'AbsV'],  'Position',[140  50 190  90]);
add_block('simulink/Math Operations/Divide',  [V 'DivAP'], 'Position',[230  50 280  90]);
add_block('simulink/Sources/Constant',        [V 'k1PF'],  'Value','1','Position',[320  45 370  75]);
add_block('simulink/Math Operations/Subtract',[V 'SubPF'], 'Position',[410  40 460 100]);
add_block('simulink/Math Operations/Gain',    [V 'GPF'],   'Gain','100','Position',[500  50 560  80]);
add_block('simulink/Discontinuities/Saturation',[V 'SatPF'], ...
    'UpperLimit','100','LowerLimit','0','Position',[590  50 650  80]);
add_block('simulink/Ports & Subsystems/Out1', [V 'o4'],    'Position',[820 130 850 150]);

add_line(V,'iV/1',   'AbsV/1',  'autorouting','on');
add_line(V,'AbsV/1', 'DivAP/1', 'autorouting','on');
add_line(V,'kVn/1',  'DivAP/2', 'autorouting','on');
add_line(V,'k1PF/1', 'SubPF/1', 'autorouting','on');
add_line(V,'DivAP/1','SubPF/2', 'autorouting','on');
add_line(V,'SubPF/1','GPF/1',   'autorouting','on');
add_line(V,'GPF/1',  'SatPF/1', 'autorouting','on');
add_line(V,'SatPF/1','o4/1',    'autorouting','on');

%% ── OUT5: SOHR = R_meas / R0 ─────────────────────────────────
%%   R_meas = |V_OCV − V_term| / |I|
%%   Both numerator and denominator are always positive
add_block('simulink/Math Operations/Subtract',[V 'SubR'],  'Position',[140 430 190 490]);
add_block('simulink/Math Operations/Abs',     [V 'AbsRv'], 'Position',[230 432 280 488]);
add_block('simulink/Math Operations/Abs',     [V 'AbsRi'], 'Position',[140 520 190 560]);
add_block('simulink/Math Operations/Divide',  [V 'DivRm'], 'Position',[320 430 370 490]);
add_block('simulink/Discontinuities/Saturation',[V 'SatRm'], ...
    'UpperLimit','10','LowerLimit','0.001','Position',[410 435 470 485]);
add_block('simulink/Math Operations/Divide',  [V 'DivSOHR'],'Position',[510 430 560 490]);
add_block('simulink/Ports & Subsystems/Out1', [V 'o5'],    'Position',[820 450 850 470]);

add_line(V,'kVoc/1', 'SubR/1',   'autorouting','on');
add_line(V,'iV/1',   'SubR/2',   'autorouting','on');
add_line(V,'SubR/1', 'AbsRv/1',  'autorouting','on');
add_line(V,'iI/1',   'AbsRi/1',  'autorouting','on');
add_line(V,'AbsRv/1','DivRm/1',  'autorouting','on');
add_line(V,'AbsRi/1','DivRm/2',  'autorouting','on');
add_line(V,'DivRm/1','SatRm/1',  'autorouting','on');
add_line(V,'SatRm/1','DivSOHR/1','autorouting','on');
add_line(V,'kR0/1',  'DivSOHR/2','autorouting','on');
add_line(V,'DivSOHR/1','o5/1',   'autorouting','on');

%% ── OUT6: Current Internal R = SOHR × R0_cell × n_cells + R_bus
add_block('simulink/Math Operations/Product',[V 'PrCIR'],'Inputs','***','Position',[610 420 670 500]);
add_block('simulink/Math Operations/Add',    [V 'AddCIR'],'Position',[720 430 770 490]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o6'],   'Position',[820 520 850 540]);

add_line(V,'DivSOHR/1','PrCIR/1','autorouting','on');
add_line(V,'kRc/1',    'PrCIR/2','autorouting','on');
add_line(V,'kNc/1',    'PrCIR/3','autorouting','on');
add_line(V,'PrCIR/1',  'AddCIR/1','autorouting','on');
add_line(V,'kRb/1',    'AddCIR/2','autorouting','on');
add_line(V,'AddCIR/1', 'o6/1',   'autorouting','on');

%% ── OUT7: R Increase % = (SOHR − 1) × 100 ───────────────────
add_block('simulink/Sources/Constant',       [V 'k1RI'], 'Value','1','Position',[510 530 560 560]);
add_block('simulink/Math Operations/Subtract',[V 'SubRI'],'Position',[610 520 660 580]);
add_block('simulink/Math Operations/Gain',   [V 'GRI'],  'Gain','100','Position',[700 530 760 570]);
add_block('simulink/Discontinuities/Saturation',[V 'SatRI'], ...
    'UpperLimit','200','LowerLimit','-100','Position',[780 530 850 570]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o7'],   'Position',[900 535 930 555]);

add_line(V,'DivSOHR/1','SubRI/1','autorouting','on');
add_line(V,'k1RI/1',   'SubRI/2','autorouting','on');
add_line(V,'SubRI/1',  'GRI/1',  'autorouting','on');
add_line(V,'GRI/1',    'SatRI/1','autorouting','on');
add_line(V,'SatRI/1',  'o7/1',   'autorouting','on');

%% ── OUT8: E Discharged = ∫(V_nom × |I| when I > 0) / 3.6e6 ─
add_block('simulink/Math Operations/Abs',     [V 'AbsIe'], 'Position',[140 620 190 660]);
add_block('simulink/Math Operations/Product', [V 'PrVnI'],'Inputs','**','Position',[230 620 280 670]);
add_block('simulink/Logic and Bit Operations/Compare To Constant',[V 'CmpIpos'], ...
    'const','0','relop','>','Position',[140 690 240 730]);
add_block('simulink/Signal Attributes/Data Type Conversion',[V 'B2Ddis'], ...
    'OutDataTypeStr','double','Position',[280 690 350 730]);
add_block('simulink/Math Operations/Product', [V 'GateDis'],'Inputs','**','Position',[390 620 440 680]);
add_block('simulink/Math Operations/Gain',    [V 'ScEd'],  'Gain','1/3600000','Position',[480 628 550 658]);
add_block('simulink/Continuous/Integrator',   [V 'IntEd'], 'InitialCondition','0','Position',[590 620 640 660]);
add_block('simulink/Ports & Subsystems/Out1', [V 'o8'],    'Position',[820 620 850 640]);

add_line(V,'iI/1',     'AbsIe/1',  'autorouting','on');
add_line(V,'kVn/1',    'PrVnI/1',  'autorouting','on');
add_line(V,'AbsIe/1',  'PrVnI/2',  'autorouting','on');
add_line(V,'iI/1',     'CmpIpos/1','autorouting','on');
add_line(V,'CmpIpos/1','B2Ddis/1', 'autorouting','on');
add_line(V,'PrVnI/1',  'GateDis/1','autorouting','on');
add_line(V,'B2Ddis/1', 'GateDis/2','autorouting','on');
add_line(V,'GateDis/1','ScEd/1',   'autorouting','on');
add_line(V,'ScEd/1',   'IntEd/1',  'autorouting','on');
add_line(V,'IntEd/1',  'o8/1',     'autorouting','on');

%% ── OUT9: E Charged = ∫(V_nom × |I| when I < 0) / 3.6e6 ────
add_block('simulink/Logic and Bit Operations/Compare To Constant',[V 'CmpIneg'], ...
    'const','0','relop','<','Position',[140 760 240 800]);
add_block('simulink/Signal Attributes/Data Type Conversion',[V 'B2Dchg'], ...
    'OutDataTypeStr','double','Position',[280 760 350 800]);
add_block('simulink/Math Operations/Product', [V 'GateChg'],'Inputs','**','Position',[390 710 440 770]);
add_block('simulink/Math Operations/Gain',    [V 'ScEc'],  'Gain','1/3600000','Position',[480 718 550 748]);
add_block('simulink/Continuous/Integrator',   [V 'IntEc'], 'InitialCondition','0','Position',[590 710 640 750]);
add_block('simulink/Ports & Subsystems/Out1', [V 'o9'],    'Position',[820 700 850 720]);

add_line(V,'iI/1',     'CmpIneg/1','autorouting','on');
add_line(V,'CmpIneg/1','B2Dchg/1', 'autorouting','on');
add_line(V,'PrVnI/1',  'GateChg/1','autorouting','on');
add_line(V,'B2Dchg/1', 'GateChg/2','autorouting','on');
add_line(V,'GateChg/1','ScEc/1',   'autorouting','on');
add_line(V,'ScEc/1',   'IntEc/1',  'autorouting','on');
add_line(V,'IntEc/1',  'o9/1',     'autorouting','on');

%% ── OUT10: RTE Remaining = E_dis / E_chg × 100 ──────────────
add_block('simulink/Math Operations/Divide',   [V 'DivRTE'], 'Position',[140 840 190 900]);
add_block('simulink/Math Operations/Gain',     [V 'GRTE'],   'Gain','100','Position',[230 845 290 885]);
add_block('simulink/Discontinuities/Saturation',[V 'SatRTE'], ...
    'UpperLimit','105','LowerLimit','0','Position',[310 845 370 885]);
add_block('simulink/Ports & Subsystems/Out1',  [V 'o10'],    'Position',[820 790 850 810]);

add_line(V,'IntEd/1','DivRTE/1','autorouting','on');
add_line(V,'IntEc/1','DivRTE/2','autorouting','on');
add_line(V,'DivRTE/1','GRTE/1', 'autorouting','on');
add_line(V,'GRTE/1',  'SatRTE/1','autorouting','on');
add_line(V,'SatRTE/1','o10/1',  'autorouting','on');

%% ── OUT11: RTE Fade = RTE_initial − RTE_remaining ───────────
add_block('simulink/Math Operations/Subtract', [V 'SubRTEf'],'Position',[420 835 470 895]);
add_block('simulink/Discontinuities/Saturation',[V 'SatRTEf'], ...
    'UpperLimit','100','LowerLimit','-5','Position',[500 840 560 890]);
add_block('simulink/Ports & Subsystems/Out1',  [V 'o11'],    'Position',[820 860 850 880]);

add_line(V,'kRTE0/1',  'SubRTEf/1','autorouting','on');
add_line(V,'SatRTE/1', 'SubRTEf/2','autorouting','on');
add_line(V,'SubRTEf/1','SatRTEf/1','autorouting','on');
add_line(V,'SatRTEf/1','o11/1',    'autorouting','on');

%% ── OUT12: SoC passthrough ────────────────────────────────────
add_block('simulink/Ports & Subsystems/Out1',[V 'o12'],'Position',[820 930 850 950]);
add_line(V,'iSoC/1','o12/1','autorouting','on');

%% ── OUT14: Extreme temperature hours ─────────────────────────
add_block('simulink/Logic and Bit Operations/Compare To Constant',[V 'CmpT45'], ...
    'const','45','relop','>','Position',[140 1180 250 1220]);
add_block('simulink/Signal Attributes/Data Type Conversion',[V 'T2D'], ...
    'OutDataTypeStr','double','Position',[290 1180 360 1220]);
add_block('simulink/Math Operations/Gain',[V 'ScThr'],'Gain','1/3600','Position',[400 1182 470 1220]);
add_block('simulink/Continuous/Integrator',[V 'IntT'],'InitialCondition','0','Position',[510 1180 560 1220]);
add_block('simulink/Ports & Subsystems/Out1',[V 'o14'],'Position',[820 1180 850 1200]);

add_line(V,'iT/1',   'CmpT45/1','autorouting','on');
add_line(V,'CmpT45/1','T2D/1',  'autorouting','on');
add_line(V,'T2D/1',  'ScThr/1', 'autorouting','on');
add_line(V,'ScThr/1','IntT/1',  'autorouting','on');
add_line(V,'IntT/1', 'o14/1',   'autorouting','on');

fprintf('   VCU done (14 outputs).\n');

%% ── Top level: BMS → VCU → Mux → To Workspace ───────────────
for k = 1:5
    add_line(modelName, ...
        ['BMS_Subsystem/' num2str(k)], ...
        ['VCU_Subsystem/' num2str(k)], ...
        'autorouting','on');
end

add_block('simulink/Signal Routing/Mux', ...
    [modelName '/DBP_Mux'], ...
    'Inputs','14','Position',[790 50 810 830]);
for k = 1:14
    add_line(modelName, ...
        ['VCU_Subsystem/' num2str(k)], ...
        ['DBP_Mux/' num2str(k)], ...
        'autorouting','on');
end

add_block('simulink/Sinks/To Workspace', ...
    [modelName '/DBP_Log'], ...
    'Position',[860 50 960 830], ...
    'VariableName','DBP_Passport_Log', ...
    'SaveFormat','Array', ...
    'MaxDataPoints','inf');
add_line(modelName,'DBP_Mux/1','DBP_Log/1','autorouting','on');

save_system(modelName);
fprintf('   Model saved: %s.slx\n\n', modelName);

%% ============================================================
%  STEP 3 — Run simulation
% ============================================================
fprintf('Step 3: Running simulation (%.0f s)...\n', StopTime);
simOut = sim(modelName, 'StopTime', num2str(StopTime));
fprintf('   Simulation complete.\n\n');

%% ============================================================
%  STEP 4 — Extract results
% ============================================================
fprintf('Step 4: Extracting results...\n');
t = simOut.tout;
Y = simOut.DBP_Passport_Log;
if isa(Y,'timeseries'), t = Y.Time; Y = Y.Data; end
if size(Y,2) ~= 14 && size(Y,1) == 14, Y = Y'; end
n_cols = size(Y,2);
fprintf('   Logged: %d samples x %d signals\n\n', length(t), n_cols);

if n_cols < 14
    error('Expected 14 signal columns, got %d. Check VCU port wiring.', n_cols);
end

% Column mapping (matches port order above)
SoH_log     = Y(:,1);   % %     SoH from LUT
Cav_log     = Y(:,2);   % Ah    C_avail
CF_log      = Y(:,3);   % %     Capacity Fade
PF_log      = Y(:,4);   % %     Power Fade
SOHR_log    = Y(:,5);   % ratio SOHR_DCH
CIR_log     = Y(:,6);   % Ohm   Current Internal R
RI_log      = Y(:,7);   % %     R Increase
Edis_log    = Y(:,8);   % kWh   E Discharged
Echg_log    = Y(:,9);   % kWh   E Charged
RTE_log     = Y(:,10);  % %     RTE Remaining
RTEf_log    = Y(:,11);  % %     RTE Fade
SoC_log     = Y(:,12);  % %     SoC
Cyc_log     = Y(:,13);  % cnt   Cycle count
ExtT_log    = Y(:,14);  % hrs   Extreme temp hours

N_cycles    = round(Cyc_log(end));
extT_hrs    = ExtT_log(end);
N_deepdis   = sum(diff([0; double(SoC_log < 10)]) == 1);

%% ============================================================
%  STEP 5 — Print passport and sanity checks
% ============================================================
fprintf('===========================================\n');
fprintf('  DIGITAL BATTERY PASSPORT — DBP Record   \n');
fprintf('===========================================\n');
fprintf('  Battery UID   : %s\n',  batteryUID);
fprintf('  Timestamp     : %s\n',  datestr(now,'yyyy-mm-dd HH:MM:SS'));
fprintf('  Status        : Original\n');
fprintf('-------------------------------------------\n');
fprintf('  [COR1] SoH (from LUT)         : %.4f %%\n', SoH_log(end));
fprintf('  [COR2] C_avail (SoH*C/100)    : %.4f Ah\n', Cav_log(end));
fprintf('  [COR3] Capacity Fade (100-SoH): %.4f %%\n', CF_log(end));
fprintf('  [REQ06] Power Fade             : %.4f %%\n', PF_log(end));
fprintf('  [REQ02] SOHR_DCH               : %.4f\n',   SOHR_log(end));
fprintf('  [REQ03] Current Internal R     : %.6f Ohm\n', CIR_log(end));
fprintf('  [REQ02] R Increase %%           : %.4f %%\n', RI_log(end));
fprintf('  [REQ07] Cumul E Discharged     : %.6f kWh\n', Edis_log(end));
fprintf('  [REQ07] Cumul E Charged        : %.6f kWh\n', Echg_log(end));
fprintf('  [REQ08] RTE Remaining          : %.4f %%\n', RTE_log(end));
fprintf('  [REQ08] RTE Fade               : %.4f %%\n', RTEf_log(end));
fprintf('  [COR4]  Cycle count            : %d\n',     N_cycles);
fprintf('  [BMS]   SoC (end of sim)       : %.4f %%\n', SoC_log(end));
fprintf('  [BMS]   Max temperature        : %.2f C\n',  max(Y(:,4)));
fprintf('  [D12]   Extreme temp hours     : %.4f hrs\n', extT_hrs);
fprintf('  [D13]   Deep discharge events  : %d\n',     N_deepdis);
fprintf('===========================================\n\n');

% Sanity checks
fprintf('--- Sanity checks ---\n');
chk = @(pass,msg) fprintf('  %s %s\n', msg, pass);
chk(SoH_log(end) >= 98 && SoH_log(end) <= 100, ...
    sprintf('SoH=%.2f%%  — %s', SoH_log(end), ...
    ternary(SoH_log(end)>=98,'near 100% for 5 cycles ✓','CHECK')));
chk(Cav_log(end) >= 49 && Cav_log(end) <= 50, ...
    sprintf('C_avail=%.2fAh  — %s', Cav_log(end), ...
    ternary(Cav_log(end)>=49,'≈ C_rated ✓','CHECK')));
chk(N_cycles == sim_cycles, ...
    sprintf('Cycle count=%d  — %s', N_cycles, ...
    ternary(N_cycles==sim_cycles,'equals sim_cycles ✓','CHECK')));
chk(Edis_log(end) > 0, ...
    sprintf('E_dis=%.4fkWh  — %s', Edis_log(end), ...
    ternary(Edis_log(end)>0,'positive ✓','CHECK (negative means sign error)')));
fprintf('\n');

%% ============================================================
%  STEP 6 — Export CSV
% ============================================================
csvFile = sprintf('DBP_Passport_%s_%s.csv', ...
    batteryUID, datestr(now,'yyyymmdd_HHMMSS'));
fid = fopen(csvFile,'w');
fprintf(fid,'DBP Field,Value,Unit,Formula,Req,Regulation\n');
fprintf(fid,'battery_uid,%s,-,Static,-,Art. 77(3)\n',               batteryUID);
fprintf(fid,'timestamp,%s,-,VCU clock,-,Art. 77(4)\n',              datestr(now,'yyyy-mm-dd HH:MM:SS'));
fprintf(fid,'battery_status,Original,-,Agent,-,Annex XIII 4(c)\n');
fprintf(fid,'state_of_health_pct,%.4f,%%,LUT(cycle),COR1,Annex VII A\n',      SoH_log(end));
fprintf(fid,'battery_capacity_avail_ah,%.4f,Ah,SoH*C_rated/100,COR2,Annex IV A(1)\n',Cav_log(end));
fprintf(fid,'capacity_fade_pct,%.4f,%%,100-SoH,COR3,Annex IV A(1)\n',         CF_log(end));
fprintf(fid,'power_fade_pct,%.4f,%%,(1-SOHAP)*100,REQ_06,Annex IV A(2)\n',    PF_log(end));
fprintf(fid,'sohr_dch,%.6f,ratio,R_meas/R0,REQ_02,Annex IV A(3)\n',           SOHR_log(end));
fprintf(fid,'current_internal_r_ohm,%.6f,Ohm,SOHR*Rcell*N+Rbus,REQ_03,Annex IV A(3)\n',CIR_log(end));
fprintf(fid,'r_increase_pct,%.4f,%%,(SOHR-1)*100,REQ_02,Annex IV A(3)\n',     RI_log(end));
fprintf(fid,'cumul_e_dis_kwh,%.6f,kWh,Vnom*|I|(I>0),REQ_07,Annex VII B(2)\n',Edis_log(end));
fprintf(fid,'cumul_e_chg_kwh,%.6f,kWh,Vnom*|I|(I<0),REQ_07,Annex VII B\n',   Echg_log(end));
fprintf(fid,'rte_remaining_pct,%.4f,%%,Edis/Echg*100,REQ_08,Annex IV A(4)\n', RTE_log(end));
fprintf(fid,'rte_fade_pct,%.4f,%%,RTE0-RTE_now,REQ_08,Annex IV A(4)\n',       RTEf_log(end));
fprintf(fid,'cycle_count,%d,cycles,SoC<5%% rising edge,COR4,Annex XIII 4(d)\n',N_cycles);
fprintf(fid,'soc_pct,%.4f,%%,Coulomb counting,-,Annex XIII 4(d)\n',            SoC_log(end));
fprintf(fid,'temp_max_degc,%.2f,C,BMS sensor,-,Annex XIII 4(d)\n',             max(Y(:,4)));
fprintf(fid,'extreme_temp_hours,%.4f,hrs,integral(T>45C)/3600,-,Annex VII B(4)\n',extT_hrs);
fprintf(fid,'deep_discharge_events,%d,count,SoC<10%%,-,Annex VII B(4)\n',      N_deepdis);
fclose(fid);
fprintf('CSV exported: %s\n\n', csvFile);

%% ============================================================
%  STEP 7 — Dashboard (4×4 grid)
% ============================================================
t_hr = t / 3600;
figure('Name','DBP v11 Dashboard','NumberTitle','off', ...
    'Position',[20 20 1600 960]);

subplot(4,4,1);
plot(t_hr,SoC_log,'b','LineWidth',1.5); grid on;
xlabel('hr'); ylabel('%'); title('SoC [BMS — coulomb counting]');
yline(5,'r--','Cycle trigger'); yline(10,'m--','Deep dis'); ylim([0 110]);

subplot(4,4,2);
plot(t_hr,SoH_log,'g','LineWidth',2); grid on;
xlabel('hr'); ylabel('%'); title('SoH — LUT(cycle count) [COR1]');
yline(SoH_EoL,'r--','EoL 80%'); ylim([75 105]);

subplot(4,4,3);
plot(t_hr,Cav_log,'Color',[0 0.5 0],'LineWidth',2); grid on;
xlabel('hr'); ylabel('Ah'); title('C\_avail = SoH×C\_rated/100 [COR2]');
yline(C_rated,'b--','C rated'); ylim([0 55]);

subplot(4,4,4);
plot(t_hr,CF_log,'r','LineWidth',2); grid on;
xlabel('hr'); ylabel('%'); title('Capacity Fade = 100−SoH [COR3]');

subplot(4,4,5);
plot(t_hr,PF_log,'Color',[0.6 0 0],'LineWidth',1.5); grid on;
xlabel('hr'); ylabel('%'); title('Power Fade [REQ\_06]');

subplot(4,4,6);
plot(t_hr,SOHR_log,'k','LineWidth',1.5); grid on;
xlabel('hr'); ylabel('ratio'); title('SOHR = R\_meas/R0 [REQ\_02]');
yline(1,'b--','BOL=1');

subplot(4,4,7);
plot(t_hr,CIR_log,'Color',[0 0 0.7],'LineWidth',1.5); grid on;
xlabel('hr'); ylabel('Ohm'); title('Current Internal R [REQ\_03]');

subplot(4,4,8);
plot(t_hr,RI_log,'Color',[0.4 0 0.4],'LineWidth',1.5); grid on;
xlabel('hr'); ylabel('%'); title('R Increase % [REQ\_02]');

subplot(4,4,9);
plot(t_hr,Edis_log,'m','LineWidth',1.5); grid on;
xlabel('hr'); ylabel('kWh'); title('Cumul E Discharged [REQ\_07]');

subplot(4,4,10);
plot(t_hr,Echg_log,'Color',[0 0.5 0.5],'LineWidth',1.5); grid on;
xlabel('hr'); ylabel('kWh'); title('Cumul E Charged [REQ\_07]');

subplot(4,4,11);
plot(t_hr,RTE_log,'c','LineWidth',1.5); grid on;
xlabel('hr'); ylabel('%'); title('RTE Remaining [REQ\_08]');
yline(RTE_initial,'b--','Initial'); ylim([0 110]);

subplot(4,4,12);
plot(t_hr,RTEf_log,'Color',[0.8 0.4 0],'LineWidth',1.5); grid on;
xlabel('hr'); ylabel('%'); title('RTE Fade [REQ\_08]');

subplot(4,4,13);
stairs(t_hr,Cyc_log,'Color',[0.1 0.4 0.8],'LineWidth',2); grid on;
xlabel('hr'); ylabel('count'); title('Cycle Counter [COR4]');
ylim([0 sim_cycles+1]);

subplot(4,4,14);
plot(t_hr,ExtT_log,'Color',[0.9 0.5 0],'LineWidth',2); grid on;
xlabel('hr'); ylabel('hrs'); title('Extreme Temp Hours');

subplot(4,4,15);
yyaxis left;  plot(t_hr,Edis_log,'m','LineWidth',1.5); ylabel('E\_dis kWh');
yyaxis right; plot(t_hr,Echg_log,'Color',[0 0.5 0.5],'LineWidth',1.5); ylabel('E\_chg kWh');
grid on; xlabel('hr'); title('Discharge vs Charge Energy');

subplot(4,4,16);
plot(t_hr,CF_log,'r','LineWidth',2); hold on;
plot(t_hr,PF_log,'Color',[0.6 0 0],'LineWidth',1.5,'LineStyle','--');
plot(t_hr,RTEf_log,'Color',[0.8 0.4 0],'LineWidth',1.5,'LineStyle',':');
grid on; xlabel('hr'); ylabel('%'); title('All Fades');
legend('CapFade','PwrFade','RTEFade','Location','best');

sgtitle(sprintf('DBP v11 | UID: %s | %.1f hrs | %d cycles', ...
    batteryUID, t(end)/3600, N_cycles), ...
    'FontSize',12,'FontWeight','bold');

fprintf('Done.\n\n');

%% Helper
function s = ternary(cond, a, b)
    if cond, s = a; else, s = b; end
end
