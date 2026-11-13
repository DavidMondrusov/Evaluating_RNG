from . import ovs, spraypoint


DEFAULT_ROUTING = "OVS controller"
ROUTING_METHODS = {
    DEFAULT_ROUTING: ovs,
    "Spraypoint": spraypoint,
}


def get_controller_class(name):
    try:
        return ROUTING_METHODS[name].controller_class()
    except KeyError as error:
        raise ValueError(
            f"Unsupported routing method {name!r}; choose from: "
            + ", ".join(ROUTING_METHODS)
        ) from error


def default_parameters(name):
    try:
        return dict(ROUTING_METHODS[name].DEFAULT_PARAMETERS)
    except KeyError as error:
        raise ValueError(
            f"Unsupported routing method {name!r}; choose from: "
            + ", ".join(ROUTING_METHODS)
        ) from error


def configure(name, net, parameters):
    try:
        routing_method = ROUTING_METHODS[name]
    except KeyError as error:
        raise ValueError(
            f"Unsupported routing method {name!r}; choose from: "
            + ", ".join(ROUTING_METHODS)
        ) from error
    return routing_method.configure(net, parameters)
