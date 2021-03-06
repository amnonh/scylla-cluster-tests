import socket
import logging
import threading
import time
import requests
try:
    from BaseHTTPServer import HTTPServer
    from SocketServer import ThreadingMixIn
except ImportError:
    # Python 3
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

import prometheus_client
from sdcm.utils.common import retrying, log_run_info
from sdcm.sct_events import PrometheusAlertManagerEvent, EVENTS_PROCESSES


START = 'start'
STOP = 'stop'


LOGGER = logging.getLogger(__name__)
NM_OBJ = None


class _ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    """Thread per request HTTP server."""


def start_http_server(port, addr='', registry=prometheus_client.REGISTRY):
    """Starts an HTTP server for prometheus metrics as a daemon thread"""
    custom_metrics_handler = prometheus_client.MetricsHandler.factory(registry)
    httpd = _ThreadingSimpleServer((addr, port), custom_metrics_handler)
    http_thread = threading.Thread(target=httpd.serve_forever)
    http_thread.daemon = True
    http_thread.start()
    return httpd


def start_metrics_server():
    """
    https://github.com/prometheus/prometheus/wiki/Default-port-allocations
    Occupied port 9389 for SCT
    """
    hostname = socket.gethostname()

    try:
        LOGGER.debug('Try to start prometheus API server')
        httpd = start_http_server(0)
        port = httpd.server_port
        ip = socket.gethostbyname(hostname)

        LOGGER.info('prometheus API server running on port: %s', port)
        return '{}:{}'.format(ip, port)
    except Exception as ex:  # pylint: disable=broad-except
        LOGGER.error('Cannot start local http metrics server: %s', ex)

    return None


def nemesis_metrics_obj():
    global NM_OBJ  # pylint: disable=global-statement
    if not NM_OBJ:
        NM_OBJ = NemesisMetrics()
    return NM_OBJ


class NemesisMetrics:

    DISRUPT_COUNTER = 'nemesis_disruptions_counter'
    DISRUPT_GAUGE = 'nemesis_disruptions_gauge'

    def __init__(self):
        super(NemesisMetrics, self).__init__()
        self._disrupt_counter = self.create_counter(self.DISRUPT_COUNTER,
                                                    'Counter for nemesis disruption methods',
                                                    ['method', 'event'])
        self._disrupt_gauge = self.create_gauge(self.DISRUPT_GAUGE,
                                                'Gauge for nemesis disruption methods',
                                                ['method'])

    @staticmethod
    def create_counter(name, desc, param_list):
        try:
            return prometheus_client.Counter(name, desc, param_list)
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.error('Cannot create metrics counter: %s', ex)
        return None

    @staticmethod
    def create_gauge(name, desc, param_list):
        try:
            return prometheus_client.Gauge(name, desc, param_list)
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.error('Cannot create metrics gauge: %s', ex)
        return None

    def event_start(self, disrupt):
        try:
            self._disrupt_counter.labels(disrupt, START).inc()  # pylint: disable=no-member
            self._disrupt_gauge.labels(disrupt).inc()  # pylint: disable=no-member
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.exception('Cannot start metrics event: %s', ex)

    def event_stop(self, disrupt):
        try:
            self._disrupt_counter.labels(disrupt, STOP).inc()  # pylint: disable=no-member
            self._disrupt_gauge.labels(disrupt).dec()  # pylint: disable=no-member
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.exception('Cannot stop metrics event: %s', ex)


class PrometheusAlertManagerListener(threading.Thread):

    def __init__(self, ip, port=9093, interval=10, stop_flag: threading.Event = None):
        super(PrometheusAlertManagerListener, self).__init__(name=self.__class__.__name__, daemon=True)
        self._alert_manager_url = f"http://{ip}:{port}/api/v2"
        self._stop_flag = stop_flag if stop_flag else threading.Event()
        self._interval = interval
        self._timeout = 600

    @property
    def is_alert_manager_up(self):
        try:
            return requests.get(f"{self._alert_manager_url}/status", timeout=3).json()['cluster']['status'] == 'ready'
        except Exception:  # pylint: disable=broad-except
            return False

    @log_run_info
    def wait_till_alert_manager_up(self):
        end_time = time.time() + self._timeout
        while time.time() < end_time and not self._stop_flag.is_set():
            if self.is_alert_manager_up:
                return
            time.sleep(30)
        if self._stop_flag.is_set():
            LOGGER.warning("Prometheus Alert Manager was asked to stop.")
        else:
            raise TimeoutError(f"Prometheus Alert Manager({self._alert_manager_url}) "
                               f"did not get up for {self._timeout}s")

    @log_run_info
    def stop(self, timeout=None):
        self._stop_flag.set()
        self.join(timeout)

    @retrying(n=10)
    def _get_alerts(self, active=False):
        if active:
            response = requests.get(f"{self._alert_manager_url}/alerts?active={int(active)}", timeout=3)
        else:
            response = requests.get(f"{self._alert_manager_url}/alerts", timeout=3)
        if response.status_code == 200:
            return response.json()
        return None

    def _publish_new_alerts(self, alerts: dict):  # pylint: disable=no-self-use
        for alert in alerts.values():
            PrometheusAlertManagerEvent(raw_alert=alert, event_type='start').publish()

    def _publish_end_of_alerts(self, alerts: dict):
        all_alerts = self._get_alerts()
        updated_dict = {}
        if all_alerts:
            for alert in all_alerts:
                fingerprint = alert.get('fingerprint', None)
                if not fingerprint:
                    continue
                updated_dict[fingerprint] = alert
        for alert in alerts.values():
            if not alert.get('endsAt', None):
                alert['endsAt'] = time.strftime("%Y-%m-%dT%H:%M:%S.0Z", time.gmtime())
            alert = updated_dict.get(alert['fingerprint'], alert)
            PrometheusAlertManagerEvent(raw_alert=alert, event_type='end').publish()

    def run(self):
        self.wait_till_alert_manager_up()
        existed = {}
        while not self._stop_flag.is_set():
            start_time = time.time()
            just_left = existed.copy()
            existing = {}
            new_ones = {}
            alerts = self._get_alerts(active=True)
            if alerts is not None:
                for alert in alerts:
                    fingerprint = alert.get('fingerprint', None)
                    if not fingerprint:
                        continue
                    existing[fingerprint] = alert
                    if fingerprint in just_left:
                        del just_left[fingerprint]
                        continue
                    new_ones[fingerprint] = alert
                existed = existing
            self._publish_new_alerts(new_ones)
            self._publish_end_of_alerts(just_left)
            delta = int((start_time + self._interval) - time.time())
            if delta > 0:
                time.sleep(int(delta))


# This is an example of how we'll send info into Prometheus,
# Currently it's not in use, since the data we want to show, doesn't fit Prometheus model,
# we are using the GrafanaAnnotator


class PrometheusDumper(threading.Thread):
    def __init__(self):
        self.stop_event = threading.Event()
        super(PrometheusDumper, self).__init__()

    def run(self):
        events_gauge = nemesis_metrics_obj().create_gauge('sct_events_gauge',
                                                          'Gauge for sct events',
                                                          ['event_type', 'type', 'severity', 'node'])

        for event_type, message_data in EVENTS_PROCESSES['MainDevice'].subscribe_events(stop_event=self.stop_event):
            events_gauge.labels(event_type,  # pylint: disable=no-member
                                getattr(message_data, 'type', ''),
                                message_data.severity,
                                getattr(message_data, 'node', '')).set(message_data.timestamp)

    def terminate(self):
        self.stop_event.set()
