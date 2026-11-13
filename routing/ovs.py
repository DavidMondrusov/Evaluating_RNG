from mininet.node import OVSController


DEFAULT_PARAMETERS = {}


def controller_class():
    return OVSController


def configure(net, parameters):
    return None
