"""
Odoo client for MCP server integration.

XML-RPC remains the default transport for existing deployments. Odoo 19+ can use
the External JSON-2 API by setting ``ODOO_TRANSPORT=json2`` and an API key.
"""

import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client
from typing import Any, cast

from .diagnostics import (
    JSON2_POSITIONAL_ARG_MAP,
    READ_ONLY_METHODS,
    sanitize_odoo_error,
)
from .model_policy import enforce_model_acl, metadata_scope

SUPPORTED_TRANSPORTS = {"xmlrpc", "json2"}


def _retry_attempts() -> int:
    """Extra attempts for read-only calls that hit connection errors (0-5)."""
    raw = os.environ.get("ODOO_MCP_RETRY_ATTEMPTS", "").strip()
    try:
        value = int(raw) if raw else 2
    except ValueError:
        value = 2
    return max(0, min(value, 5))


def _retry_backoff_seconds() -> float:
    """Base backoff delay in seconds; doubles per retry (capped at 10s)."""
    raw = os.environ.get("ODOO_MCP_RETRY_BACKOFF", "").strip()
    try:
        value = float(raw) if raw else 0.5
    except ValueError:
        value = 0.5
    return max(0.0, min(value, 10.0))


class OdooJson2Error(ValueError):
    """Structured JSON-2 error with redacted debug details by default."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        odoo_error: dict[str, Any] | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.odoo_error = odoo_error
        self.response_body = response_body


class OdooClient:
    """Client for interacting with Odoo via XML-RPC or JSON-2."""

    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        password: str,
        timeout: int = 10,
        verify_ssl: bool = True,
        transport: str = "xmlrpc",
        api_key: str | None = None,
        json2_database_header: bool = True,
        lang: str | None = None,
        instance: str = "default",
    ) -> None:
        """
        Initialize the Odoo client with connection parameters

        Args:
            url: Odoo server URL (with or without protocol)
            db: Database name
            username: Login username
            password: Login password or API key for explicit JSON-2 usage
            timeout: Connection timeout in seconds
            verify_ssl: Whether to verify SSL certificates
            transport: Transport backend, either ``xmlrpc`` or ``json2``
            api_key: Odoo API key used as JSON-2 bearer token
            json2_database_header: Whether JSON-2 calls should send X-Odoo-Database
            lang: Default Odoo locale to inject into context.lang
                (caller-supplied context.lang always wins).
            instance: Configured instance name; scopes the model ACL policy.
        """
        # Ensure URL has a protocol
        if not re.match(r"^https?://", url):
            url = f"http://{url}"

        # Remove trailing slash from URL if present
        url = url.rstrip("/")

        self.url = url
        self.db = db
        self.username = username
        self.password = password
        self.uid: int | None = None
        self.transport = normalize_transport(transport)
        self.api_key = api_key or (password if self.transport == "json2" else None)
        self.json2_database_header = json2_database_header
        self.lang = (lang or "").strip() or None
        self.instance = instance

        # Set timeout and SSL verification
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        # Setup connections
        self._common: Any = None
        self._models: Any = None

        # Parse hostname for logging
        parsed_url = urllib.parse.urlparse(self.url)
        self.hostname = parsed_url.netloc

        # Connect
        self._connect()

    def _connect(self) -> None:
        """Initialize the selected transport and authenticate."""
        print(f"Connecting to Odoo at: {self.url}", file=sys.stderr)
        print(f"  Hostname: {self.hostname}", file=sys.stderr)
        print(f"  Transport: {self.transport}", file=sys.stderr)
        print(
            f"  Timeout: {self.timeout}s, Verify SSL: {self.verify_ssl}",
            file=sys.stderr,
        )

        if self.transport == "json2":
            self._connect_json2()
        else:
            self._connect_xmlrpc()

    def _connect_xmlrpc(self) -> None:
        """Initialize the XML-RPC connection and authenticate."""
        # Create a transport with the configured timeout.
        is_https = self.url.startswith("https://")
        transport = RedirectTransport(
            timeout=self.timeout, use_https=is_https, verify_ssl=self.verify_ssl
        )

        # Set up XML-RPC endpoints. allow_none=True is required so the client can
        # marshal None (Odoo's XML-RPC server already emits <nil/>); without it a
        # write payload or fields_get metadata containing None raises
        # "cannot marshal None unless allow_none is enabled".
        self._common = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", transport=transport, allow_none=True
        )
        self._models = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object", transport=transport, allow_none=True
        )

        # Authenticate and capture the user ID.
        print(
            f"Authenticating with database: {self.db}, username: {self.username}",
            file=sys.stderr,
        )
        try:
            print(
                f"Making request to {self.hostname}/xmlrpc/2/common (attempt 1)",
                file=sys.stderr,
            )
            self.uid = self._common.authenticate(
                self.db, self.username, self.password, {}
            )
            if not self.uid:
                raise ValueError("Authentication failed: Invalid username or password")
        except (socket.error, socket.timeout, ConnectionError, TimeoutError) as e:
            print(f"Connection error: {str(e)}", file=sys.stderr)
            raise ConnectionError(f"Failed to connect to Odoo server: {str(e)}")
        except Exception as e:
            print(f"Authentication error: {str(e)}", file=sys.stderr)
            raise ValueError(f"Failed to authenticate with Odoo: {str(e)}")

    def _connect_json2(self) -> None:
        """Validate JSON-2 bearer authentication with a lightweight call."""
        if not self.api_key:
            raise ValueError(
                "JSON-2 transport requires ODOO_API_KEY, or ODOO_PASSWORD containing an Odoo API key."
            )

        print("Authenticating with JSON-2 bearer token", file=sys.stderr)
        try:
            self._json2_call("res.users", "context_get", {})
        except (socket.error, socket.timeout, ConnectionError, TimeoutError) as e:
            print(f"Connection error: {str(e)}", file=sys.stderr)
            raise ConnectionError(f"Failed to connect to Odoo server: {str(e)}")
        except Exception as e:
            print(f"Authentication error: {str(e)}", file=sys.stderr)
            raise ValueError(f"Failed to authenticate with Odoo JSON-2: {str(e)}")

    def _apply_lang_context(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Merge ``self.lang`` into ``kwargs['context']`` when caller did not set it."""
        if not self.lang:
            return kwargs
        merged = dict(kwargs)
        existing_context = merged.get("context")
        context = dict(existing_context) if isinstance(existing_context, dict) else {}
        if "lang" not in context:
            context["lang"] = self.lang
            merged["context"] = context
        return merged

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a method on an Odoo model.

        Read-only ORM methods retry on connection-level failures with
        exponential backoff (ODOO_MCP_RETRY_ATTEMPTS / ODOO_MCP_RETRY_BACKOFF).
        Anything that may mutate data is never retried.

        Every RPC passes the model ACL choke point first (see model_policy).
        """
        enforce_model_acl(getattr(self, "instance", "default"), model, method)
        kwargs = self._apply_lang_context(kwargs)
        extra_attempts = _retry_attempts() if method in READ_ONLY_METHODS else 0
        last_error: Exception | None = None
        for attempt in range(1 + extra_attempts):
            if attempt:
                delay = _retry_backoff_seconds() * (2 ** (attempt - 1))
                print(
                    f"Retrying {model}.{method} after connection error "
                    f"(attempt {attempt + 1}, waiting {delay:.1f}s)",
                    file=sys.stderr,
                )
                time.sleep(delay)
            try:
                return self._execute_once(model, method, args, kwargs)
            except (
                ConnectionError,
                TimeoutError,
                socket.error,
                xmlrpc.client.ProtocolError,
            ) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _execute_once(
        self,
        model: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Single transport-level execution without retry logic."""
        if self.transport == "json2":
            payload = self._build_json2_payload(model, method, args, kwargs)
            return self._json2_call(model, method, payload)

        return self._models.execute_kw(
            self.db, self.uid, self.password, model, method, list(args), kwargs
        )

    def _build_json2_payload(
        self,
        model: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert common XML-RPC style positional calls to JSON-2 named args."""
        payload = dict(kwargs)
        if not args:
            return payload

        arg_names = JSON2_POSITIONAL_ARG_MAP.get(method)
        if arg_names is None:
            raise ValueError(
                f"JSON-2 transport requires keyword arguments for {model}.{method}; "
                "positional arguments are only mapped for common ORM methods. "
                "Pass kwargs matching the Odoo method signature or use XML-RPC."
            )

        if len(args) > len(arg_names):
            raise ValueError(
                f"JSON-2 transport received too many positional arguments for "
                f"{model}.{method}: expected at most {len(arg_names)}, got {len(args)}."
            )

        for name, value in zip(arg_names, args):
            if name in payload:
                raise ValueError(
                    f"JSON-2 transport received {model}.{method} argument {name!r} "
                    "both positionally and as a keyword."
                )
            payload[name] = value

        return payload

    def _json2_call(self, model: str, method: str, payload: dict[str, Any]) -> Any:
        """POST a JSON-2 request and return the decoded JSON result."""
        if not self.api_key:
            raise ValueError("JSON-2 API key is not configured")

        endpoint = f"{self.url}/json/2/{model}/{method}"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.json2_database_header and self.db:
            headers["X-Odoo-Database"] = self.db

        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers=headers,
        )
        context = (
            ssl._create_unverified_context()
            if self.url.startswith("https://") and not self.verify_ssl
            else None
        )

        try:
            print(
                f"Making JSON-2 request to {self.hostname}/json/2/{model}/{method}",
                file=sys.stderr,
            )
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=context,
            ) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            error_body = err.read().decode("utf-8", errors="replace")
            odoo_error = sanitize_odoo_error(error_body)
            message = (
                f"JSON-2 request {model}.{method} failed with HTTP {err.code}: "
                f"{odoo_error.get('message') if odoo_error else error_body}"
            )
            raise OdooJson2Error(
                message,
                status_code=err.code,
                odoo_error=odoo_error,
                response_body=error_body,
            ) from err
        except urllib.error.URLError as err:
            raise ConnectionError(
                f"JSON-2 request {model}.{method} failed: {err.reason}"
            ) from err

        if not response_body:
            return None

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as err:
            raise ValueError(
                f"JSON-2 request {model}.{method} returned invalid JSON: {response_body}"
            ) from err

    def execute_method(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        Execute an arbitrary method on a model

        Args:
            model: The model name (e.g., 'res.partner')
            method: Method name to execute
            *args: Positional arguments to pass to the method
            **kwargs: Keyword arguments to pass to the method

        Returns:
            Result of the method execution
        """
        return self._execute(model, method, *args, **kwargs)

    def get_server_version(self) -> dict[str, Any]:
        """Return Odoo server version metadata using the safest available route."""
        try:
            if self.transport == "xmlrpc" and self._common is not None:
                version_info = self._common.version()
                return cast(dict[str, Any], version_info)
            return self._http_get_json("/web/version")
        except Exception as e:
            print(f"Error retrieving server version: {str(e)}", file=sys.stderr)
            return {"error": str(e)}

    def get_user_context(self) -> dict[str, Any]:
        """Return the current user's Odoo context."""
        try:
            with metadata_scope():
                context = self._execute("res.users", "context_get")
            return cast(dict[str, Any], context)
        except Exception as e:
            print(f"Error retrieving user context: {str(e)}", file=sys.stderr)
            return {"error": str(e)}

    def get_installed_modules(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return installed module names and labels with a bounded limit."""
        try:
            with metadata_scope():
                result = self._execute(
                    "ir.module.module",
                    "search_read",
                    [["state", "=", "installed"]],
                    fields=["name", "shortdesc", "state"],
                    limit=limit,
                    order="name ASC",
                )
            return cast(list[dict[str, Any]], result)
        except Exception as e:
            print(f"Error retrieving installed modules: {str(e)}", file=sys.stderr)
            return []

    def get_profile(self, module_limit: int = 100) -> dict[str, Any]:
        """Return a bounded profile of the connected Odoo environment."""
        modules = self.get_installed_modules(limit=module_limit)
        return {
            "url": self.url,
            "hostname": self.hostname,
            "database": self.db,
            "username": self.username,
            "transport": self.transport,
            "timeout": self.timeout,
            "verify_ssl": self.verify_ssl,
            "json2_database_header": self.json2_database_header,
            "server_version": self.get_server_version(),
            "user_context": self.get_user_context(),
            "installed_modules": modules,
            "installed_module_count": len(modules),
        }

    def _http_get_json(self, path: str) -> dict[str, Any]:
        """Read a JSON HTTP endpoint from the Odoo base URL."""
        request = urllib.request.Request(
            f"{self.url}{path}",
            method="GET",
            headers={"Accept": "application/json"},
        )
        context = (
            ssl._create_unverified_context()
            if self.url.startswith("https://") and not self.verify_ssl
            else None
        )
        with urllib.request.urlopen(
            request,
            timeout=self.timeout,
            context=context,
        ) as response:
            body = response.read().decode("utf-8")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} did not return a JSON object")
        return payload

    def get_models(self) -> dict[str, Any]:
        """
        Get a list of all available models in the system

        Returns:
            List of model names

        Examples:
            >>> client = OdooClient(url, db, username, password)
            >>> models = client.get_models()
            >>> print(len(models))
            125
            >>> print(models[:5])
            ['res.partner', 'res.users', 'res.company', 'res.groups', 'ir.model']
        """
        try:
            # First search for model IDs
            with metadata_scope():
                model_ids = self._execute("ir.model", "search", [])

            if not model_ids:
                return {
                    "model_names": [],
                    "models_details": {},
                    "error": "No models found",
                }

            # Then read the model data with only the most basic fields
            # that are guaranteed to exist in all Odoo versions
            with metadata_scope():
                result = self._execute(
                    "ir.model", "read", model_ids, ["model", "name"]
                )

            # Extract and sort model names alphabetically
            models = sorted([rec["model"] for rec in result])

            # For more detailed information, include the full records
            models_info = {
                "model_names": models,
                "models_details": {
                    rec["model"]: {"name": rec.get("name", "")} for rec in result
                },
            }

            return models_info
        except Exception as e:
            print(f"Error retrieving models: {str(e)}", file=sys.stderr)
            return {"model_names": [], "models_details": {}, "error": str(e)}

    def get_model_info(self, model_name: str) -> dict[str, Any]:
        """
        Get information about a specific model

        Args:
            model_name: Name of the model (e.g., 'res.partner')

        Returns:
            Dictionary with model information

        Examples:
            >>> client = OdooClient(url, db, username, password)
            >>> info = client.get_model_info('res.partner')
            >>> print(info['name'])
            'Contact'
        """
        try:
            with metadata_scope():
                result = self._execute(
                    "ir.model",
                    "search_read",
                    [("model", "=", model_name)],
                    fields=["name", "model"],
                )

            if not result:
                return {"error": f"Model {model_name} not found"}

            return cast(dict[str, Any], result[0])
        except Exception as e:
            print(f"Error retrieving model info: {str(e)}", file=sys.stderr)
            return {"error": str(e)}

    # fields_get attributes requested by get_model_fields. Bounded on purpose: Odoo 19's XML-RPC
    # layer fails server-side ("cannot marshal None") on models whose full fields_get contains a
    # None-valued attribute (e.g. `domain` on product.pricelist), which broke validate_write for
    # those models. This list covers every attribute the server consumes; a missing attribute reads
    # as None via .get(), exactly as before.
    FIELDS_GET_ATTRIBUTES = [
        "string",
        "help",
        "type",
        "required",
        "readonly",
        "relation",
        "selection",
        "store",
        "searchable",
    ]

    def get_model_fields(self, model_name: str) -> dict[str, Any]:
        """
        Get field definitions for a specific model

        Args:
            model_name: Name of the model (e.g., 'res.partner')

        Returns:
            Dictionary mapping field names to their definitions

        Examples:
            >>> client = OdooClient(url, db, username, password)
            >>> fields = client.get_model_fields('res.partner')
            >>> print(fields['name']['type'])
            'char'
        """
        try:
            with metadata_scope():
                fields = self._execute(
                    model_name, "fields_get", attributes=self.FIELDS_GET_ATTRIBUTES
                )
            return cast(dict[str, Any], fields)
        except Exception as e:
            print(f"Error retrieving fields: {str(e)}", file=sys.stderr)
            return {"error": str(e)}

    def search_read(
        self,
        model_name: str,
        domain: list[Any],
        fields: list[str] | None = None,
        offset: int | None = None,
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search for records and read their data in a single call

        Args:
            model_name: Name of the model (e.g., 'res.partner')
            domain: Search domain (e.g., [('is_company', '=', True)])
            fields: List of field names to return (None for all)
            offset: Number of records to skip
            limit: Maximum number of records to return
            order: Sorting criteria (e.g., 'name ASC, id DESC')

        Returns:
            List of dictionaries with the matching records

        Examples:
            >>> client = OdooClient(url, db, username, password)
            >>> records = client.search_read('res.partner', [('is_company', '=', True)], limit=5)
            >>> print(len(records))
            5
        """
        kwargs: dict[str, Any] = {}
        if offset:
            kwargs["offset"] = offset
        if fields is not None:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order

        result = self._execute(model_name, "search_read", domain, **kwargs)
        return cast(list[dict[str, Any]], result)

    def read_records(
        self, model_name: str, ids: list[int], fields: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """
        Read data of records by IDs

        Args:
            model_name: Name of the model (e.g., 'res.partner')
            ids: List of record IDs to read
            fields: List of field names to return (None for all)

        Returns:
            List of dictionaries with the requested records

        Examples:
            >>> client = OdooClient(url, db, username, password)
            >>> records = client.read_records('res.partner', [1])
            >>> print(records[0]['name'])
            'YourCompany'
        """
        kwargs: dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields

        result = self._execute(model_name, "read", ids, **kwargs)
        return cast(list[dict[str, Any]], result)


class RedirectTransport(xmlrpc.client.Transport):
    """Transport that adds timeout, SSL verification, and redirect handling"""

    def __init__(
        self,
        timeout: int = 10,
        use_https: bool = True,
        verify_ssl: bool = True,
        max_redirects: int = 5,
        proxy: str | None = None,
    ) -> None:
        super().__init__()
        self.timeout = timeout
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.max_redirects = max_redirects
        self.proxy = proxy or os.environ.get("HTTP_PROXY")
        self.context: Any = None

        if use_https and not verify_ssl:
            import ssl

            self.context = ssl._create_unverified_context()

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        if isinstance(host, tuple):
            host = host[0]
        host = str(host)

        if self.proxy:
            proxy_url = urllib.parse.urlparse(self.proxy)
            if not proxy_url.hostname:
                raise ValueError("Invalid HTTP_PROXY value")
            proxy_port = proxy_url.port or 80
            connection = http.client.HTTPConnection(
                proxy_url.hostname, proxy_port, timeout=self.timeout
            )
            connection.set_tunnel(host)
        else:
            if self.use_https and not self.verify_ssl:
                connection = http.client.HTTPSConnection(
                    host, timeout=self.timeout, context=self.context
                )
            else:
                if self.use_https:
                    connection = http.client.HTTPSConnection(host, timeout=self.timeout)
                else:
                    connection = http.client.HTTPConnection(host, timeout=self.timeout)

        return connection

    def request(
        self, host: Any, handler: str, request_body: Any, verbose: bool = False
    ) -> Any:
        """Send HTTP request with retry for redirects"""
        redirects = 0
        while redirects < self.max_redirects:
            try:
                print(f"Making request to {host}{handler}", file=sys.stderr)
                return super().request(host, handler, request_body, verbose)
            except xmlrpc.client.ProtocolError as err:
                if err.errcode in (301, 302, 303, 307, 308) and err.headers.get(
                    "location"
                ):
                    redirects += 1
                    location_header = err.headers.get("location")
                    if isinstance(location_header, bytes):
                        location = location_header.decode()
                    else:
                        location = str(location_header)
                    parsed = urllib.parse.urlparse(location)
                    if parsed.netloc:
                        host = parsed.netloc
                    handler = parsed.path
                    if parsed.query:
                        handler += "?" + parsed.query
                else:
                    raise
            except Exception as e:
                print(f"Error during request: {str(e)}", file=sys.stderr)
                raise

        host_label = host[0] if isinstance(host, tuple) else str(host)
        raise xmlrpc.client.ProtocolError(
            host_label + handler, 310, "Too many redirects", {}
        )


def normalize_transport(transport: str) -> str:
    """Normalize transport aliases to stable internal identifiers."""
    normalized = transport.strip().lower().replace("-", "").replace("_", "")
    if normalized == "xmlrpc":
        return "xmlrpc"
    if normalized == "json2":
        return "json2"
    raise ValueError(
        f"Unsupported Odoo transport {transport!r}. Expected one of: {', '.join(sorted(SUPPORTED_TRANSPORTS))}."
    )


INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

LEGACY_ENV_VARS = ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_PASSWORD")


def _env_config() -> dict[str, Any] | None:
    """Build a config entry from legacy environment variables, if complete."""
    if not all(var in os.environ for var in LEGACY_ENV_VARS):
        return None
    config: dict[str, Any] = {
        "url": os.environ["ODOO_URL"],
        "db": os.environ["ODOO_DB"],
        "username": os.environ["ODOO_USERNAME"],
        "password": os.environ["ODOO_PASSWORD"],
    }
    if "ODOO_TRANSPORT" in os.environ:
        config["transport"] = os.environ["ODOO_TRANSPORT"]
    if "ODOO_API_KEY" in os.environ:
        config["api_key"] = os.environ["ODOO_API_KEY"]
    if "ODOO_JSON2_DATABASE_HEADER" in os.environ:
        config["json2_database_header"] = os.environ["ODOO_JSON2_DATABASE_HEADER"]
    if "ODOO_LOCALE" in os.environ:
        config["lang"] = os.environ["ODOO_LOCALE"]
    return config


def _apply_legacy_env_overrides(entry: dict[str, Any]) -> dict[str, Any]:
    """Apply env-over-file overrides for legacy flat (single-instance) configs.

    Multi-instance entries never receive env credential/transport overrides;
    this preserves the historical precedence for flat configs only.
    """
    merged = dict(entry)
    if "ODOO_TRANSPORT" in os.environ:
        merged["transport"] = os.environ["ODOO_TRANSPORT"]
    if "ODOO_API_KEY" in os.environ:
        merged["api_key"] = os.environ["ODOO_API_KEY"]
    if "ODOO_JSON2_DATABASE_HEADER" in os.environ:
        merged["json2_database_header"] = os.environ["ODOO_JSON2_DATABASE_HEADER"]
    if "ODOO_LOCALE" in os.environ:
        merged["lang"] = os.environ["ODOO_LOCALE"]
    return merged


def _config_file_paths() -> list[str]:
    """Config file candidates: explicit ODOO_CONFIG_FILE first, then standard paths."""
    paths = []
    explicit = os.environ.get("ODOO_CONFIG_FILE")
    if explicit:
        expanded = os.path.expanduser(explicit)
        if not os.path.exists(expanded):
            raise FileNotFoundError(
                f"ODOO_CONFIG_FILE points to a missing file: {explicit}"
            )
        paths.append(expanded)
    paths.extend(
        [
            "./odoo_config.json",
            os.path.expanduser("~/.config/odoo/config.json"),
            os.path.expanduser("~/.odoo_config.json"),
        ]
    )
    return paths


def _validate_instances(
    raw: dict[str, Any], path: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Validate a multi-instance config shape. Error messages never include values."""
    instances = raw.get("instances")
    if not isinstance(instances, dict) or not instances:
        raise ValueError(f"Config file {path}: 'instances' must be a non-empty object.")
    for name, entry in instances.items():
        if not INSTANCE_NAME_RE.match(name):
            raise ValueError(
                f"Config file {path}: invalid instance name {name!r} "
                "(allowed: letters, digits, '_', '-', max 64 characters)."
            )
        if not isinstance(entry, dict):
            raise ValueError(
                f"Config file {path}: instance {name!r} must be an object."
            )
        for required in ("url", "db"):
            if not entry.get(required):
                raise ValueError(
                    f"Config file {path}: instance {name!r} is missing "
                    f"required key '{required}'."
                )
    default = raw.get("default")
    if default is None:
        if len(instances) == 1:
            default = next(iter(instances))
        else:
            raise ValueError(
                f"Config file {path}: 'default' is required when multiple "
                f"instances are defined. Available instances: {sorted(instances)}"
            )
    if default not in instances:
        raise ValueError(
            f"Config file {path}: default instance {default!r} not found. "
            f"Available instances: {sorted(instances)}"
        )
    return str(default), instances


def load_instances_config() -> tuple[str, dict[str, dict[str, Any]]]:
    """
    Load named Odoo instance configurations.

    Returns:
        tuple: (default_instance_name, {name: config_entry}). Legacy environment
        variables or a flat config file yield a single instance named "default".
    """
    env_config = _env_config()
    if env_config is not None:
        if os.environ.get("ODOO_CONFIG_FILE"):
            print(
                "Warning: ODOO_CONFIG_FILE is ignored because ODOO_URL/ODOO_DB/"
                "ODOO_USERNAME/ODOO_PASSWORD are all set; the environment "
                "defines a single 'default' instance. Unset them to use the "
                "config file.",
                file=sys.stderr,
            )
        return "default", {"default": env_config}

    for path in _config_file_paths():
        expanded_path = os.path.expanduser(path)
        if not os.path.exists(expanded_path):
            continue
        try:
            with open(expanded_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in Odoo config file {expanded_path}: "
                f"line {e.lineno} column {e.colno}"
            ) from e
        if isinstance(raw, dict) and "instances" in raw:
            return _validate_instances(raw, expanded_path)
        return "default", {
            "default": _apply_legacy_env_overrides(cast(dict[str, Any], raw))
        }

    raise FileNotFoundError(
        "No Odoo configuration found. Please create an odoo_config.json file, "
        "set ODOO_CONFIG_FILE, or set environment variables."
    )


def load_config() -> dict[str, str]:
    """
    Load Odoo configuration from environment variables or config file

    Returns:
        dict: Configuration dictionary with url, db, username, password
    """
    default_name, instances = load_instances_config()
    return cast(dict[str, str], instances[default_name])


def build_odoo_client(entry: dict[str, Any], *, name: str = "default") -> OdooClient:
    """Build an OdooClient from a config entry, with env vars as fallback defaults."""
    timeout = int(entry.get("timeout") or os.environ.get("ODOO_TIMEOUT", "30"))
    verify_ssl_raw = entry.get("verify_ssl")
    if verify_ssl_raw is None:
        verify_ssl_raw = os.environ.get("ODOO_VERIFY_SSL", "1")
    verify_ssl = (
        verify_ssl_raw
        if isinstance(verify_ssl_raw, bool)
        else parse_bool(str(verify_ssl_raw))
    )
    transport = normalize_transport(str(entry.get("transport") or "xmlrpc"))
    # Credentials never fall back to env vars: an instance entry without its own
    # api_key must not inherit another deployment's key (legacy flat/env configs
    # receive env overrides upstream in load_instances_config/_env_config).
    api_key = entry.get("api_key")
    json2_header_raw = entry.get("json2_database_header")
    if json2_header_raw is None:
        json2_header_raw = "1"
    json2_database_header = (
        json2_header_raw
        if isinstance(json2_header_raw, bool)
        else parse_bool(str(json2_header_raw))
    )
    lang = entry.get("lang") or os.environ.get("ODOO_LOCALE") or None

    # Print detailed configuration (never credentials)
    print(f"Odoo client configuration [instance: {name}]:", file=sys.stderr)
    print(f"  URL: {entry['url']}", file=sys.stderr)
    print(f"  Database: {entry['db']}", file=sys.stderr)
    print(f"  Username: {entry.get('username', '')}", file=sys.stderr)
    print(f"  Transport: {transport}", file=sys.stderr)
    print(f"  Timeout: {timeout}s", file=sys.stderr)
    print(f"  Verify SSL: {verify_ssl}", file=sys.stderr)
    print(f"  JSON-2 database header: {json2_database_header}", file=sys.stderr)
    if lang:
        print(f"  Default locale: {lang}", file=sys.stderr)

    return OdooClient(
        url=entry["url"],
        db=entry["db"],
        username=str(entry.get("username", "")),
        password=str(entry.get("password", "")),
        timeout=timeout,
        verify_ssl=verify_ssl,
        transport=transport,
        api_key=api_key,
        json2_database_header=json2_database_header,
        lang=lang,
        instance=name,
    )


def get_odoo_client() -> OdooClient:
    """
    Get a configured Odoo client instance for the default instance

    Returns:
        OdooClient: A configured Odoo client instance
    """
    default_name, instances = load_instances_config()
    return build_odoo_client(instances[default_name], name=default_name)


def get_odoo_client_for(instance: str | None = None) -> tuple[str, OdooClient]:
    """
    Get a configured Odoo client for a named instance (or the default).

    Raises:
        ValueError: If the instance name is not configured. The message lists
            available instance names only — never URLs or credentials.
    """
    default_name, instances = load_instances_config()
    name = instance or default_name
    if name not in instances:
        raise ValueError(
            f"Unknown Odoo instance {name!r}. "
            f"Available instances: {sorted(instances)}"
        )
    return name, build_odoo_client(instances[name], name=name)


def list_configured_instances() -> dict[str, dict[str, Any]]:
    """Describe configured instances without exposing credentials."""
    default_name, instances = load_instances_config()
    summary: dict[str, dict[str, Any]] = {}
    for name, entry in instances.items():
        # Explicit allowlist of keys — never copy the raw entry.
        raw_tags = entry.get("tags") or []
        cross = entry.get("cross_instance")
        summary[name] = {
            "url": entry.get("url"),
            "db": entry.get("db"),
            "transport": normalize_transport(str(entry.get("transport") or "xmlrpc")),
            "is_default": name == default_name,
            "tags": [str(t) for t in raw_tags] if isinstance(raw_tags, list) else [],
            "cross_instance": True if cross is None else bool(cross),
        }
    return summary


def parse_bool(value: str) -> bool:
    """Parse common environment/config boolean values."""
    return value.strip().lower() in ["1", "true", "yes", "on"]
