use futures::StreamExt;
use futures::channel::mpsc::{UnboundedReceiver, UnboundedSender, unbounded};
use futures::channel::oneshot;
use futures::future::poll_fn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyModule, PyString};
use rmb::runtime::mdns::{MdnsBackend, set_mdns_backend as set_mdns_backend_rmb};
use rmb::{
    BasicInfoConfig, Bridge, BridgeConfig, BridgeEvent, ClusterPreset, ClusterRef, CommissioningInfo,
    DatapointSelector, DatapointValue, DeviceConfig, DeviceConfigBuilder, DeviceHandle,
    DeviceTypeRef, EndpointPreset, EnergyMeasurementData, MatterError, SwitchEvent,
    known_cluster_refs, known_device_type_refs,
};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex as StdMutex};
use std::task::Poll;
use std::thread;
use std::time::Duration;

type RunnerFuture = Pin<Box<dyn Future<Output = Result<(), MatterError>> + 'static>>;
type WorkerJoin = Arc<StdMutex<Option<thread::JoinHandle<()>>>>;

fn start_worker(
    storage_path: String,
    vendor_id: Option<u16>,
    product_id: Option<u16>,
    vendor_name: Option<String>,
    product_name: Option<String>,
    serial_number: Option<String>,
) -> PyResult<(UnboundedSender<WorkerCommand>, WorkerJoin)> {
    let (tx, rx) = unbounded::<WorkerCommand>();
    let (ready_tx, ready_rx) = std_mpsc::channel();
    let join: WorkerJoin = Arc::new(StdMutex::new(Some(thread::spawn(move || {
        let mut basic_info = BasicInfoConfig::new();
        if let Some(vid) = vendor_id {
            basic_info.vid = vid;
        }
        if let Some(pid) = product_id {
            basic_info.pid = pid;
        }
        if let Some(vn) = vendor_name {
            basic_info.vendor_name = Box::leak(vn.into_boxed_str());
        }
        if let Some(pn) = product_name {
            basic_info.product_name = Box::leak(pn.into_boxed_str());
        }
        if let Some(sn) = serial_number {
            basic_info.serial_no = Box::leak(sn.into_boxed_str());
        }
        let basic_info = Box::leak(Box::new(basic_info));

        let config = BridgeConfig::new(PathBuf::from(storage_path)).basic_info(basic_info);
        match Bridge::new(config) {
            Ok(bridge) => {
                let _ = ready_tx.send(Ok(()));
                worker_loop(bridge, rx);
            }
            Err(err) => {
                let _ = ready_tx.send(Err(err.to_string()));
            }
        }
    }))));
    match ready_rx.recv() {
        Ok(Ok(())) => Ok((tx, join)),
        Ok(Err(err)) => Err(PyRuntimeError::new_err(err)),
        Err(_) => Err(PyRuntimeError::new_err("worker thread failed to start")),
    }
}

enum WorkerTick {
    Command(Option<WorkerCommand>),
    Event(Option<BridgeEvent>),
    Runner(Result<(), MatterError>),
}

fn worker_loop(bridge: Bridge, mut cmd_rx: UnboundedReceiver<WorkerCommand>) {
    let mut bridge = Some(bridge);
    let mut devices: std::collections::HashMap<u64, DeviceHandle> =
        std::collections::HashMap::new();
    let mut next_handle_id: u64 = 1;
    let mut events_rx: Option<UnboundedReceiver<BridgeEvent>> = None;
    let mut event_tx: Option<std_mpsc::Sender<BridgeEvent>> = None;
    let mut runner: Option<RunnerFuture> = None;
    let mut started = false;

    loop {
        let tick = async_io::block_on(poll_fn(|cx| {
            if let Poll::Ready(cmd) = cmd_rx.poll_next_unpin(cx) {
                return Poll::Ready(WorkerTick::Command(cmd));
            }
            if let Some(events) = events_rx.as_mut()
                && let Poll::Ready(evt) = events.poll_next_unpin(cx)
            {
                return Poll::Ready(WorkerTick::Event(evt));
            }
            if let Some(runner) = runner.as_mut()
                && let Poll::Ready(res) = runner.as_mut().poll(cx)
            {
                return Poll::Ready(WorkerTick::Runner(res));
            }
            Poll::Pending
        }));

        match tick {
            WorkerTick::Command(cmd) => match cmd {
                Some(WorkerCommand::IsCommissioned { reply }) => {
                    let res = bridge
                        .as_ref()
                        .ok_or_else(|| "bridge already started".to_string())
                        .map(|bridge| bridge.is_commissioned());
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::OpenCommissioningWindowQr { reply }) => {
                    let res = bridge
                        .as_mut()
                        .ok_or_else(|| "bridge already started".to_string())
                        .and_then(|bridge| {
                            bridge
                                .open_commissioning_window_qr()
                                .map_err(|e| e.to_string())
                        });
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::ReopenCommissioningWindowQr { reply }) => {
                    let res = bridge
                        .as_mut()
                        .ok_or_else(|| "bridge already started".to_string())
                        .and_then(|bridge| {
                            bridge
                                .reopen_commissioning_window_qr()
                                .map_err(|e| e.to_string())
                        });
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::ResetCommissioning { reply }) => {
                    let res = bridge
                        .as_mut()
                        .ok_or_else(|| "bridge already started".to_string())
                        .and_then(|bridge| bridge.reset_commissioning().map_err(|e| e.to_string()));
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::AddDevice { cfg, reply }) => {
                    let res = bridge
                        .as_mut()
                        .ok_or_else(|| "bridge already started".to_string())
                        .map(|bridge| bridge.add_device(cfg));
                    let dev = match res {
                        Ok(dev) => dev,
                        Err(err) => {
                            let _ = reply.send(Err(err));
                            continue;
                        }
                    };
                    let handle_id = next_handle_id;
                    next_handle_id += 1;
                    let info = DeviceInfo {
                        handle_id,
                        id: dev.id(),
                        label: dev.label().to_string(),
                        endpoints: dev.endpoints().to_vec(),
                    };
                    devices.insert(handle_id, dev);
                    let _ = reply.send(Ok(info));
                }
                Some(WorkerCommand::Start {
                    event_tx: tx,
                    reply,
                }) => {
                    if started {
                        let _ = reply.send(Err("bridge already started".to_string()));
                        continue;
                    }
                    let res = bridge
                        .take()
                        .ok_or_else(|| "bridge already started".to_string())
                        .map(|bridge| bridge.start());
                    let (events, _handle, runner_future) = match res {
                        Ok(res) => res,
                        Err(err) => {
                            let _ = reply.send(Err(err));
                            continue;
                        }
                    };
                    events_rx = Some(events);
                    event_tx = Some(tx);
                    runner = Some(Box::pin(runner_future));
                    started = true;
                    let _ = reply.send(Ok(()));
                }
                Some(WorkerCommand::SetAttribute {
                    handle_id,
                    selector,
                    value,
                    reply,
                }) => {
                    let res = devices
                        .get(&handle_id)
                        .ok_or_else(|| "unknown device handle".to_string())
                        .and_then(|dev| dev.set(selector, value).map_err(|e| format!("{e:?}")));
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::UpdateAttribute {
                    handle_id,
                    selector,
                    value,
                    reply,
                }) => {
                    let res = devices
                        .get(&handle_id)
                        .ok_or_else(|| "unknown device handle".to_string())
                        .and_then(|dev| dev.update(selector, value).map_err(|e| format!("{e:?}")));
                    let _ = reply.send(res);
                }
                Some(WorkerCommand::Stop { reply }) => {
                    let _ = reply.send(());
                    break;
                }
                None => break,
            },
            WorkerTick::Event(evt) => {
                if let Some(evt) = evt
                    && let Some(tx) = event_tx.as_ref()
                {
                    let _ = tx.send(evt);
                }
            }
            WorkerTick::Runner(_res) => {
                runner = None;
            }
        }
    }
}

fn send_request<T, F>(tx: &UnboundedSender<WorkerCommand>, build: F) -> PyResult<T>
where
    F: FnOnce(oneshot::Sender<Result<T, String>>) -> WorkerCommand,
{
    let (reply_tx, reply_rx) = oneshot::channel();
    tx.unbounded_send(build(reply_tx))
        .map_err(|_| PyRuntimeError::new_err("worker thread stopped"))?;
    let result = async_io::block_on(reply_rx)
        .map_err(|_| PyRuntimeError::new_err("worker thread stopped"))?;
    result.map_err(PyRuntimeError::new_err)
}

fn send_stop(tx: &UnboundedSender<WorkerCommand>) -> PyResult<()> {
    let (reply_tx, reply_rx) = oneshot::channel();
    tx.unbounded_send(WorkerCommand::Stop { reply: reply_tx })
        .map_err(|_| PyRuntimeError::new_err("worker thread stopped"))?;
    let _ = async_io::block_on(reply_rx);
    Ok(())
}

#[pyclass(name = "Bridge")]
struct PyBridge {
    tx: UnboundedSender<WorkerCommand>,
    join: Arc<StdMutex<Option<thread::JoinHandle<()>>>>,
}

#[pyclass(name = "DeviceHandle")]
struct PyDeviceHandle {
    handle_id: u64,
    id: u16,
    label: String,
    endpoints: Vec<u16>,
    tx: UnboundedSender<WorkerCommand>,
}

#[pyclass(unsendable, name = "DeviceConfig")]
struct PyDeviceConfig {
    inner: DeviceConfig,
}

#[pyclass(unsendable, name = "DeviceConfigBuilder")]
struct PyDeviceConfigBuilder {
    inner: StdMutex<Option<DeviceConfigBuilder>>,
}

#[pyclass(from_py_object, unsendable, name = "EndpointPreset")]
#[derive(Clone)]
struct PyEndpointPreset {
    inner: EndpointPreset,
}

#[pyclass(from_py_object, frozen, name = "DeviceTypeRef")]
#[derive(Clone, Copy)]
struct PyDeviceTypeRef {
    inner: DeviceTypeRef,
}

#[pyclass(from_py_object, frozen, name = "ClusterRef")]
#[derive(Clone, Copy)]
struct PyClusterRef {
    inner: ClusterRef,
}

#[pyclass(name = "EventStream")]
struct PyEventStream {
    inner: StdMutex<std_mpsc::Receiver<BridgeEvent>>,
}

#[pyclass(name = "Runner")]
struct PyRunner {
    tx: UnboundedSender<WorkerCommand>,
    join: Arc<StdMutex<Option<thread::JoinHandle<()>>>>,
}

struct DeviceInfo {
    handle_id: u64,
    id: u16,
    label: String,
    endpoints: Vec<u16>,
}

enum WorkerCommand {
    IsCommissioned {
        reply: oneshot::Sender<Result<bool, String>>,
    },
    OpenCommissioningWindowQr {
        reply: oneshot::Sender<Result<Option<CommissioningInfo>, String>>,
    },
    ReopenCommissioningWindowQr {
        reply: oneshot::Sender<Result<Option<CommissioningInfo>, String>>,
    },
    ResetCommissioning {
        reply: oneshot::Sender<Result<Option<CommissioningInfo>, String>>,
    },
    AddDevice {
        cfg: DeviceConfig,
        reply: oneshot::Sender<Result<DeviceInfo, String>>,
    },
    Start {
        event_tx: std_mpsc::Sender<BridgeEvent>,
        reply: oneshot::Sender<Result<(), String>>,
    },
    SetAttribute {
        handle_id: u64,
        selector: DatapointSelector,
        value: DatapointValue,
        reply: oneshot::Sender<Result<(), String>>,
    },
    UpdateAttribute {
        handle_id: u64,
        selector: DatapointSelector,
        value: DatapointValue,
        reply: oneshot::Sender<Result<(), String>>,
    },
    Stop {
        reply: oneshot::Sender<()>,
    },
}

#[pymethods]
impl PyBridge {
    #[new]
    #[pyo3(signature = (storage_path, vendor_id=None, product_id=None, vendor_name=None, product_name=None, serial_number=None))]
    fn new(
        storage_path: String,
        vendor_id: Option<u16>,
        product_id: Option<u16>,
        vendor_name: Option<String>,
        product_name: Option<String>,
        serial_number: Option<String>,
    ) -> PyResult<Self> {
        let (tx, join) = start_worker(
            storage_path,
            vendor_id,
            product_id,
            vendor_name,
            product_name,
            serial_number,
        )?;
        Ok(Self { tx, join })
    }

    fn is_commissioned(&self) -> PyResult<bool> {
        send_request(&self.tx, |reply| WorkerCommand::IsCommissioned { reply })
    }

    fn open_commissioning_window_qr(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let info = send_request(&self.tx, |reply| WorkerCommand::OpenCommissioningWindowQr {
            reply,
        })?;
        Ok(info
            .map(|info| commissioning_info_to_py(py, info))
            .unwrap_or_else(|| py.None()))
    }

    fn reopen_commissioning_window_qr(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let info = send_request(&self.tx, |reply| {
            WorkerCommand::ReopenCommissioningWindowQr { reply }
        })?;
        Ok(info
            .map(|info| commissioning_info_to_py(py, info))
            .unwrap_or_else(|| py.None()))
    }

    fn reset_commissioning(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let info = send_request(&self.tx, |reply| WorkerCommand::ResetCommissioning {
            reply,
        })?;
        Ok(info
            .map(|info| commissioning_info_to_py(py, info))
            .unwrap_or_else(|| py.None()))
    }

    fn add_device(&self, cfg: &PyDeviceConfig) -> PyResult<PyDeviceHandle> {
        let info = send_request(&self.tx, |reply| WorkerCommand::AddDevice {
            cfg: cfg.inner.clone(),
            reply,
        })?;
        Ok(PyDeviceHandle {
            handle_id: info.handle_id,
            id: info.id,
            label: info.label,
            endpoints: info.endpoints,
            tx: self.tx.clone(),
        })
    }

    fn events(&self) -> PyResult<PyEventStream> {
        Err(PyRuntimeError::new_err(
            "events() is not supported in worker mode; use start()",
        ))
    }

    fn start(&self) -> PyResult<(PyEventStream, PyRunner)> {
        let (event_tx, event_rx) = std_mpsc::channel();
        send_request(&self.tx, |reply| WorkerCommand::Start { event_tx, reply })?;
        Ok((
            PyEventStream {
                inner: StdMutex::new(event_rx),
            },
            PyRunner {
                tx: self.tx.clone(),
                join: self.join.clone(),
            },
        ))
    }

    fn run(&self) -> PyResult<()> {
        let (_events, runner) = self.start()?;
        runner.join()
    }
}

#[pymethods]
impl PyDeviceHandle {
    fn id(&self) -> PyResult<u16> {
        Ok(self.id)
    }

    fn label(&self) -> PyResult<String> {
        Ok(self.label.clone())
    }

    fn endpoints(&self) -> PyResult<Vec<u16>> {
        Ok(self.endpoints.clone())
    }

    #[pyo3(signature = (endpoint_id, cluster_id, attribute_id, kind_or_value, value=None))]
    fn set_attribute(
        &self,
        endpoint_id: u16,
        cluster_id: u32,
        attribute_id: u32,
        kind_or_value: &Bound<'_, PyAny>,
        value: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let value = match value {
            Some(value) => {
                let kind: String = kind_or_value.extract()?;
                datapoint_value_from_kind(&kind, value)?
            }
            None => datapoint_value_infer(kind_or_value)?,
        };
        let selector = DatapointSelector::Attribute {
            endpoint_id,
            cluster_id,
            attribute_id,
        };
        send_request(&self.tx, |reply| WorkerCommand::SetAttribute {
            handle_id: self.handle_id,
            selector,
            value,
            reply,
        })
        .map(|_| ())
    }

    #[pyo3(signature = (endpoint_id, cluster_id, attribute_id, kind_or_value, value=None))]
    fn update_attribute(
        &self,
        endpoint_id: u16,
        cluster_id: u32,
        attribute_id: u32,
        kind_or_value: &Bound<'_, PyAny>,
        value: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let value = match value {
            Some(value) => {
                let kind: String = kind_or_value.extract()?;
                datapoint_value_from_kind(&kind, value)?
            }
            None => datapoint_value_infer(kind_or_value)?,
        };
        let selector = DatapointSelector::Attribute {
            endpoint_id,
            cluster_id,
            attribute_id,
        };
        send_request(&self.tx, |reply| WorkerCommand::UpdateAttribute {
            handle_id: self.handle_id,
            selector,
            value,
            reply,
        })
        .map(|_| ())
    }
}

#[pymethods]
impl PyEventStream {
    fn recv(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let guard = self.inner.lock().unwrap();
        match guard.recv() {
            Ok(evt) => Ok(bridge_event_to_py(py, evt)),
            Err(_) => Ok(py.None()),
        }
    }

    fn try_recv(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let guard = self.inner.lock().unwrap();
        match guard.try_recv() {
            Ok(evt) => Ok(bridge_event_to_py(py, evt)),
            Err(_) => Ok(py.None()),
        }
    }

    fn recv_timeout(&self, py: Python<'_>, timeout_ms: u64) -> PyResult<Py<PyAny>> {
        let guard = self.inner.lock().unwrap();
        match guard.recv_timeout(Duration::from_millis(timeout_ms)) {
            Ok(evt) => Ok(bridge_event_to_py(py, evt)),
            Err(_) => Ok(py.None()),
        }
    }
}

#[pymethods]
impl PyRunner {
    fn stop(&self) -> PyResult<()> {
        send_stop(&self.tx)?;
        Ok(())
    }

    fn join(&self) -> PyResult<()> {
        let handle = self.join.lock().unwrap().take();
        if let Some(handle) = handle {
            handle
                .join()
                .map_err(|_| PyRuntimeError::new_err("worker thread panicked"))?;
        }
        Ok(())
    }
}

#[pymethods]
impl PyDeviceConfigBuilder {
    #[new]
    fn new(label: String) -> Self {
        Self {
            inner: StdMutex::new(Some(DeviceConfigBuilder::new(&label))),
        }
    }

    fn id(&self, id: u16) -> PyResult<()> {
        self.with_builder(|b| b.id(id));
        Ok(())
    }

    fn vendor_name(&self, name: String) -> PyResult<()> {
        self.with_builder(|b| b.vendor_name(&name));
        Ok(())
    }

    fn product_name(&self, name: String) -> PyResult<()> {
        self.with_builder(|b| b.product_name(&name));
        Ok(())
    }

    fn serial_number(&self, number: String) -> PyResult<()> {
        self.with_builder(|b| b.serial_number(&number));
        Ok(())
    }

    fn endpoint(&self, endpoint: &PyEndpointPreset) -> PyResult<()> {
        let preset = endpoint.inner.clone();
        self.with_builder(|b| b.endpoint(preset));
        Ok(())
    }

    fn endpoints(&self, endpoints: Vec<PyRef<'_, PyEndpointPreset>>) -> PyResult<()> {
        let list = endpoints
            .iter()
            .map(|ep| ep.inner.clone())
            .collect::<Vec<_>>();
        self.with_builder(|b| b.endpoints(list));
        Ok(())
    }

    fn build(&self) -> PyResult<PyDeviceConfig> {
        let builder = self.take_builder()?;
        let cfg = builder.build();
        Ok(PyDeviceConfig { inner: cfg })
    }
}

impl PyDeviceConfigBuilder {
    fn with_builder<F>(&self, f: F)
    where
        F: FnOnce(DeviceConfigBuilder) -> DeviceConfigBuilder,
    {
        let mut guard = self.inner.lock().unwrap();
        let builder = guard.take().unwrap();
        *guard = Some(f(builder));
    }

    fn take_builder(&self) -> PyResult<DeviceConfigBuilder> {
        let mut guard = self.inner.lock().unwrap();
        guard
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("builder already consumed"))
    }
}

#[pymethods]
impl PyEndpointPreset {
    #[new]
    fn new(device_type: PyDeviceTypeRef) -> Self {
        Self {
            inner: EndpointPreset::new(device_type.inner),
        }
    }

    fn endpoint_id(&mut self, endpoint_id: u16) -> PyResult<()> {
        self.inner = self.inner.clone().endpoint_id(endpoint_id);
        Ok(())
    }

    fn clusters(&mut self, preset: &str) -> PyResult<()> {
        let preset = parse_cluster_preset(preset)?;
        self.inner = self.inner.clone().clusters(preset);
        Ok(())
    }

    fn cluster_list(&mut self, clusters: Vec<PyRef<'_, PyClusterRef>>) -> PyResult<()> {
        let list = clusters.iter().map(|c| c.inner).collect::<Vec<_>>();
        self.inner = self.inner.clone().cluster_list(list);
        Ok(())
    }

    fn node_label(&mut self, label: String) -> PyResult<()> {
        self.inner = self.inner.clone().node_label(&label);
        Ok(())
    }

    fn bridged(&mut self, bridged: bool) -> PyResult<()> {
        self.inner = self.inner.clone().bridged(bridged);
        Ok(())
    }

    fn direct(&mut self) -> PyResult<()> {
        self.inner = self.inner.clone().direct();
        Ok(())
    }
}

#[pymethods]
impl PyDeviceTypeRef {
    #[new]
    fn new(dtype: u16, drev: u16) -> Self {
        Self {
            inner: DeviceTypeRef::new(dtype, drev),
        }
    }

    fn dtype(&self) -> u16 {
        self.inner.dtype
    }

    fn drev(&self) -> u16 {
        self.inner.drev
    }

    fn name(&self) -> Option<String> {
        self.inner.name.map(|name| name.to_string())
    }
}

#[pymethods]
impl PyClusterRef {
    #[new]
    fn new(id: u32) -> Self {
        Self {
            inner: ClusterRef::new(id),
        }
    }

    fn id(&self) -> u32 {
        self.inner.id
    }

    fn name(&self) -> Option<String> {
        self.inner.name.map(|name| name.to_string())
    }
}

#[pyfunction]
fn device_type(name: &str) -> PyResult<PyDeviceTypeRef> {
    let normalized = normalize_name(name);
    let dtype = match normalized.as_str() {
        "root_node" => DeviceTypeRef::root_node(),
        "aggregator" => DeviceTypeRef::aggregator(),
        "bridged_node" => DeviceTypeRef::bridged_node(),
        "on_off_light" => DeviceTypeRef::on_off_light(),
        "dimmable_light" => DeviceTypeRef::dimmable_light(),
        "extended_color_light" => DeviceTypeRef::extended_color_light(),
        "smart_speaker" => DeviceTypeRef::smart_speaker(),
        "casting_video_player" => DeviceTypeRef::casting_video_player(),
        "video_player" => DeviceTypeRef::video_player(),
        "light_sensor" => DeviceTypeRef::light_sensor(),
        "occupancy_sensor" => DeviceTypeRef::occupancy_sensor(),
        "contact_sensor" => DeviceTypeRef::contact_sensor(),
        "temperature_sensor" => DeviceTypeRef::temperature_sensor(),
        "humidity_sensor" => DeviceTypeRef::humidity_sensor(),
        "pressure_sensor" => DeviceTypeRef::pressure_sensor(),
        "flow_sensor" => DeviceTypeRef::flow_sensor(),
        "thermostat" => DeviceTypeRef::thermostat(),
        "fan" => DeviceTypeRef::fan(),
        "window_covering" => DeviceTypeRef::window_covering(),
        "door_lock" => DeviceTypeRef::door_lock(),
        "generic_switch" => DeviceTypeRef::generic_switch(),
        "air_quality_sensor" => DeviceTypeRef::air_quality_sensor(),
        "smoke_co_alarm" => DeviceTypeRef::smoke_co_alarm(),
        "water_leak_detector" => DeviceTypeRef::water_leak_detector(),
        "electrical_sensor" => DeviceTypeRef::electrical_sensor(),
        _ => {
            return Err(PyRuntimeError::new_err(format!(
                "unknown device type: {name}"
            )));
        }
    };
    Ok(PyDeviceTypeRef { inner: dtype })
}

#[pyfunction]
fn cluster(name: &str) -> PyResult<PyClusterRef> {
    let normalized = normalize_name(name);
    let cluster = match normalized.as_str() {
        "identify" => ClusterRef::identify(),
        "groups" => ClusterRef::groups(),
        "scenes_management" => ClusterRef::scenes_management(),
        "on_off" => ClusterRef::on_off(),
        "switch" => ClusterRef::switch(),
        "level_control" => ClusterRef::level_control(),
        "door_lock" => ClusterRef::door_lock(),
        "window_covering" => ClusterRef::window_covering(),
        "thermostat" => ClusterRef::thermostat(),
        "fan_control" => ClusterRef::fan_control(),
        "thermostat_user_interface_configuration" => {
            ClusterRef::thermostat_user_interface_configuration()
        }
        "color_control" => ClusterRef::color_control(),
        "ballast_configuration" => ClusterRef::ballast_configuration(),
        "illuminance_measurement" => ClusterRef::illuminance_measurement(),
        "temperature_measurement" => ClusterRef::temperature_measurement(),
        "pressure_measurement" => ClusterRef::pressure_measurement(),
        "flow_measurement" => ClusterRef::flow_measurement(),
        "relative_humidity_measurement" => ClusterRef::relative_humidity_measurement(),
        "occupancy_sensing" => ClusterRef::occupancy_sensing(),
        "boolean_state" => ClusterRef::boolean_state(),
        "smoke_co_alarm" => ClusterRef::smoke_co_alarm(),
        "air_quality" => ClusterRef::air_quality(),
        "carbon_monoxide_concentration_measurement" => {
            ClusterRef::carbon_monoxide_concentration_measurement()
        }
        "carbon_dioxide_concentration_measurement" => {
            ClusterRef::carbon_dioxide_concentration_measurement()
        }
        "nitrogen_dioxide_concentration_measurement" => {
            ClusterRef::nitrogen_dioxide_concentration_measurement()
        }
        "ozone_concentration_measurement" => ClusterRef::ozone_concentration_measurement(),
        "formaldehyde_concentration_measurement" => {
            ClusterRef::formaldehyde_concentration_measurement()
        }
        "pm1_concentration_measurement" => ClusterRef::pm1_concentration_measurement(),
        "pm25_concentration_measurement" => ClusterRef::pm25_concentration_measurement(),
        "pm10_concentration_measurement" => ClusterRef::pm10_concentration_measurement(),
        "radon_concentration_measurement" => ClusterRef::radon_concentration_measurement(),
        "total_volatile_organic_compounds_concentration_measurement" => {
            ClusterRef::total_volatile_organic_compounds_concentration_measurement()
        }
        "electrical_energy_measurement" => ClusterRef::electrical_energy_measurement(),
        "electrical_power_measurement" => ClusterRef::electrical_power_measurement(),
        "power_topology" => ClusterRef::power_topology(),
        "power_source" => ClusterRef::power_source(),
        "keypad_input" => ClusterRef::keypad_input(),
        "content_launcher" => ClusterRef::content_launcher(),
        "audio_output" => ClusterRef::audio_output(),
        "application_launcher" => ClusterRef::application_launcher(),
        "application_basic" => ClusterRef::application_basic(),
        "account_login" => ClusterRef::account_login(),
        "target_navigator" => ClusterRef::target_navigator(),
        "media_playback" => ClusterRef::media_playback(),
        "media_input" => ClusterRef::media_input(),
        "low_power" => ClusterRef::low_power(),
        "wake_on_lan" => ClusterRef::wake_on_lan(),
        "channel" => ClusterRef::channel(),
        "dishwasher_mode" => ClusterRef::dishwasher_mode(),
        "laundry_washer_mode" => ClusterRef::laundry_washer_mode(),
        "refrigerator_and_temperature_controlled_cabinet_mode" => {
            ClusterRef::refrigerator_and_temperature_controlled_cabinet_mode()
        }
        "operational_state" => ClusterRef::operational_state(),
        "rvc_run_mode" => ClusterRef::rvc_run_mode(),
        "rvc_clean_mode" => ClusterRef::rvc_clean_mode(),
        "rvc_operational_state" => ClusterRef::rvc_operational_state(),
        "hepa_filter_monitoring" => ClusterRef::hepa_filter_monitoring(),
        "activated_carbon_filter_monitoring" => ClusterRef::activated_carbon_filter_monitoring(),
        _ => return Err(PyRuntimeError::new_err(format!("unknown cluster: {name}"))),
    };
    Ok(PyClusterRef { inner: cluster })
}

#[pyfunction]
fn known_clusters() -> PyResult<Vec<(String, u32)>> {
    Ok(known_cluster_refs()
        .into_iter()
        .map(|c| (c.name.unwrap_or("Unknown").to_string(), c.id))
        .collect())
}

#[pyfunction]
fn known_device_types() -> PyResult<Vec<(String, u16, u16)>> {
    Ok(known_device_type_refs()
        .into_iter()
        .map(|d| (d.name.unwrap_or("Unknown").to_string(), d.dtype, d.drev))
        .collect())
}

#[pyfunction]
fn set_mdns_backend(name: &str) -> PyResult<()> {
    let normalized = normalize_name(name);
    match normalized.as_str() {
        "zeroconf" => set_mdns_backend_rmb(MdnsBackend::Zeroconf)
            .map_err(|err| PyRuntimeError::new_err(err.to_string())),
        _ => Err(PyRuntimeError::new_err(format!(
            "unknown mdns backend: {name}"
        ))),
    }
}

#[pymodule]
fn matterbridge(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyBridge>()?;
    m.add_class::<PyDeviceHandle>()?;
    m.add_class::<PyDeviceConfig>()?;
    m.add_class::<PyDeviceConfigBuilder>()?;
    m.add_class::<PyEndpointPreset>()?;
    m.add_class::<PyDeviceTypeRef>()?;
    m.add_class::<PyClusterRef>()?;
    m.add_class::<PyEventStream>()?;
    m.add_class::<PyRunner>()?;
    m.add_function(wrap_pyfunction!(device_type, m)?)?;
    m.add_function(wrap_pyfunction!(cluster, m)?)?;
    m.add_function(wrap_pyfunction!(known_clusters, m)?)?;
    m.add_function(wrap_pyfunction!(known_device_types, m)?)?;
    m.add_function(wrap_pyfunction!(set_mdns_backend, m)?)?;
    Ok(())
}

fn commissioning_info_to_py(py: Python<'_>, info: CommissioningInfo) -> Py<PyAny> {
    let dict = PyDict::new(py);
    let _ = dict.set_item("pairing_code", info.pairing_code);
    let _ = dict.set_item("qr_code_text", info.qr_code_text);
    let _ = dict.set_item("qr_code", info.qr_code);
    dict.into_any().unbind()
}

fn bridge_event_to_py(py: Python<'_>, evt: BridgeEvent) -> Py<PyAny> {
    let dict = PyDict::new(py);
    match evt {
        BridgeEvent::OnOff { endpoint_id, on } => {
            let _ = dict.set_item("type", "on_off");
            let _ = dict.set_item("endpoint_id", endpoint_id);
            let _ = dict.set_item("on", on);
        }
        BridgeEvent::LevelControl { endpoint_id, level } => {
            let _ = dict.set_item("type", "level_control");
            let _ = dict.set_item("endpoint_id", endpoint_id);
            let _ = dict.set_item("level", level);
        }
        BridgeEvent::CommandReceived {
            endpoint_id,
            cluster_id,
            command_id,
            payload,
        } => {
            let _ = dict.set_item("type", "command_received");
            let _ = dict.set_item("endpoint_id", endpoint_id);
            let _ = dict.set_item("cluster_id", cluster_id);
            let _ = dict.set_item("command_id", command_id);
            let _ = dict.set_item("payload", PyBytes::new(py, &payload));
        }
        BridgeEvent::AttributeUpdated {
            endpoint_id,
            cluster_id,
            attribute_id,
            value,
        } => {
            let _ = dict.set_item("type", "attribute_updated");
            let _ = dict.set_item("endpoint_id", endpoint_id);
            let _ = dict.set_item("cluster_id", cluster_id);
            let _ = dict.set_item("attribute_id", attribute_id);
            let payload: Option<Py<PyAny>> =
                value.map(|v| PyBytes::new(py, &v).into_any().unbind());
            let _ = dict.set_item("value", payload);
        }
        BridgeEvent::Switch { endpoint_id, event } => {
            let _ = dict.set_item("type", "switch");
            let _ = dict.set_item("endpoint_id", endpoint_id);
            let _ = dict.set_item("event", switch_event_to_py(py, event));
        }
    }
    dict.into_any().unbind()
}

fn switch_event_to_py(py: Python<'_>, event: SwitchEvent) -> Py<PyAny> {
    let dict = PyDict::new(py);
    match event {
        SwitchEvent::SwitchLatched { new_position } => {
            let _ = dict.set_item("type", "switch_latched");
            let _ = dict.set_item("new_position", new_position);
        }
        SwitchEvent::InitialPress { new_position } => {
            let _ = dict.set_item("type", "initial_press");
            let _ = dict.set_item("new_position", new_position);
        }
        SwitchEvent::LongPress { new_position } => {
            let _ = dict.set_item("type", "long_press");
            let _ = dict.set_item("new_position", new_position);
        }
        SwitchEvent::ShortRelease { previous_position } => {
            let _ = dict.set_item("type", "short_release");
            let _ = dict.set_item("previous_position", previous_position);
        }
        SwitchEvent::LongRelease { previous_position } => {
            let _ = dict.set_item("type", "long_release");
            let _ = dict.set_item("previous_position", previous_position);
        }
        SwitchEvent::MultiPressOngoing {
            new_position,
            current_number_of_presses_counted,
        } => {
            let _ = dict.set_item("type", "multi_press_ongoing");
            let _ = dict.set_item("new_position", new_position);
            let _ = dict.set_item(
                "current_number_of_presses_counted",
                current_number_of_presses_counted,
            );
        }
        SwitchEvent::MultiPressComplete {
            previous_position,
            total_number_of_presses_counted,
        } => {
            let _ = dict.set_item("type", "multi_press_complete");
            let _ = dict.set_item("previous_position", previous_position);
            let _ = dict.set_item(
                "total_number_of_presses_counted",
                total_number_of_presses_counted,
            );
        }
    }
    dict.into_any().unbind()
}

fn normalize_name(name: &str) -> String {
    name.to_ascii_lowercase().replace('-', "_")
}

fn parse_cluster_preset(preset: &str) -> PyResult<ClusterPreset> {
    let normalized = normalize_name(preset);
    match normalized.as_str() {
        "all" => Ok(ClusterPreset::All),
        "on_off_light" => Ok(ClusterPreset::OnOffLight),
        "fan" => Ok(ClusterPreset::Fan),
        "generic_switch" => Ok(ClusterPreset::GenericSwitch),
        "temperature_sensor" => Ok(ClusterPreset::TemperatureSensor),
        "thermostat_user_interface_configuration" => {
            Ok(ClusterPreset::ThermostatUserInterfaceConfiguration)
        }
        _ => Err(PyRuntimeError::new_err(format!(
            "unknown cluster preset: {preset}"
        ))),
    }
}

fn datapoint_value_from_kind(kind: &str, value: &Bound<'_, PyAny>) -> PyResult<DatapointValue> {
    let normalized = normalize_name(kind);
    match normalized.as_str() {
        "bool" => Ok(DatapointValue::Bool(value.extract()?)),
        "u8" => Ok(DatapointValue::U8(value.extract()?)),
        "u16" => Ok(DatapointValue::U16(value.extract()?)),
        "u32" => Ok(DatapointValue::U32(value.extract()?)),
        "i8" => Ok(DatapointValue::I8(value.extract()?)),
        "i16" => Ok(DatapointValue::I16(value.extract()?)),
        "f32" => Ok(DatapointValue::F32(value.extract()?)),
        "string" => Ok(DatapointValue::String(value.extract()?)),
        "dishwasher_mode" => Ok(DatapointValue::DishwasherMode(value.extract()?)),
        "laundry_washer_mode" => Ok(DatapointValue::LaundryWasherMode(value.extract()?)),
        "refrigerator_mode" => Ok(DatapointValue::RefrigeratorMode(value.extract()?)),
        "rvc_run_mode" => Ok(DatapointValue::RvcRunMode(value.extract()?)),
        "rvc_clean_mode" => Ok(DatapointValue::RvcCleanMode(value.extract()?)),
        "operational_state" => Ok(DatapointValue::OperationalState(value.extract()?)),
        "filter_condition" => Ok(DatapointValue::FilterCondition(value.extract()?)),
        "filter_change_indication" => Ok(DatapointValue::FilterChangeIndication(value.extract()?)),
        "filter_in_place" => Ok(DatapointValue::FilterInPlace(value.extract()?)),
        "electrical_energy_measurement" => Ok(DatapointValue::ElectricalEnergyMeasurement(
            energy_measurement_from_py(value)?,
        )),
        _ => Err(PyRuntimeError::new_err(format!(
            "unknown value kind: {kind}"
        ))),
    }
}

fn energy_measurement_from_py(value: &Bound<'_, PyAny>) -> PyResult<Option<EnergyMeasurementData>> {
    if value.is_none() {
        return Ok(None);
    }
    let dict = value.cast::<PyDict>()?;
    let energy_item = dict.get_item("energy")?;
    let energy: i64 = match energy_item {
        Some(value) => value.extract()?,
        None => {
            return Err(PyRuntimeError::new_err(
                "electrical_energy_measurement requires energy",
            ));
        }
    };
    let start_timestamp = extract_optional_u32(dict, "start_timestamp")?;
    let end_timestamp = extract_optional_u32(dict, "end_timestamp")?;
    let start_systime = extract_optional_u64(dict, "start_systime")?;
    let end_systime = extract_optional_u64(dict, "end_systime")?;
    let apparent_energy = extract_optional_i64(dict, "apparent_energy")?;
    let reactive_energy = extract_optional_i64(dict, "reactive_energy")?;
    Ok(Some(EnergyMeasurementData {
        energy,
        start_timestamp,
        end_timestamp,
        start_systime,
        end_systime,
        apparent_energy,
        reactive_energy,
    }))
}

fn extract_optional_u32(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u32>> {
    match dict.get_item(key)? {
        Some(value) if value.is_none() => Ok(None),
        Some(value) => Ok(Some(value.extract::<u32>()?)),
        None => Ok(None),
    }
}

fn extract_optional_u64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u64>> {
    match dict.get_item(key)? {
        Some(value) if value.is_none() => Ok(None),
        Some(value) => Ok(Some(value.extract::<u64>()?)),
        None => Ok(None),
    }
}

fn extract_optional_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(value) if value.is_none() => Ok(None),
        Some(value) => Ok(Some(value.extract::<i64>()?)),
        None => Ok(None),
    }
}

fn datapoint_value_infer(value: &Bound<'_, PyAny>) -> PyResult<DatapointValue> {
    if value.is_instance_of::<PyBool>() {
        return Ok(DatapointValue::Bool(value.extract()?));
    }

    if let Ok(raw) = value.extract::<i128>() {
        if raw >= 0 {
            if raw <= u8::MAX as i128 {
                return Ok(DatapointValue::U8(raw as u8));
            }
            if raw <= u16::MAX as i128 {
                return Ok(DatapointValue::U16(raw as u16));
            }
            if raw <= u32::MAX as i128 {
                return Ok(DatapointValue::U32(raw as u32));
            }
            return Err(PyRuntimeError::new_err(
                "integer out of range; provide kind for larger values",
            ));
        }

        if raw >= i8::MIN as i128 {
            return Ok(DatapointValue::I8(raw as i8));
        }
        if raw >= i16::MIN as i128 {
            return Ok(DatapointValue::I16(raw as i16));
        }

        return Err(PyRuntimeError::new_err(
            "integer out of range; provide kind for larger values",
        ));
    }

    if value.is_instance_of::<PyFloat>() {
        return Ok(DatapointValue::F32(value.extract()?));
    }

    if value.is_instance_of::<PyString>() {
        return Ok(DatapointValue::String(value.extract()?));
    }

    Err(PyRuntimeError::new_err(
        "unable to infer datapoint type; provide kind string",
    ))
}
