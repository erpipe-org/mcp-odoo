"""Unit tests for tool_helpers module validators and parsers."""

import pytest

from odoo_mcp import tool_helpers


class TestValidateModelName:
    """Test validate_model_name with happy path and adversarial inputs."""

    def test_valid_simple_model_name(self):
        """Accept standard Odoo model names."""
        tool_helpers.validate_model_name("res.partner")
        tool_helpers.validate_model_name("sale.order")
        tool_helpers.validate_model_name("ir.module.module")

    def test_valid_single_word_model(self):
        """Accept single-word model names like 'wizard'."""
        tool_helpers.validate_model_name("wizard")
        tool_helpers.validate_model_name("report")

    def test_valid_with_underscores(self):
        """Accept model names with underscores."""
        tool_helpers.validate_model_name("res.partner_category")
        tool_helpers.validate_model_name("sale_order_line")

    def test_valid_starting_with_underscore(self):
        """Accept model names starting with underscore."""
        tool_helpers.validate_model_name("_internal.model")

    def test_invalid_empty_string(self):
        """Reject empty model names."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("")

    def test_invalid_starting_with_digit(self):
        """Reject model names starting with digit."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("123.model")

    def test_invalid_with_spaces(self):
        """Reject model names containing spaces."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res partner")

    def test_invalid_with_special_chars(self):
        """Reject model names with special characters."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res@partner")
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res-partner")
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res/partner")

    def test_sql_injection_attempt(self):
        """Reject SQL-like injection vectors."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res'; DROP TABLE--")
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res' OR '1'='1")

    def test_unicode_in_model_name(self):
        """Reject unicode characters in model names."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("résultat.partenaire")
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("res.合作伙伴")

    def test_path_traversal_attempt(self):
        """Reject path traversal patterns."""
        with pytest.raises(ValueError, match="Invalid model name"):
            tool_helpers.validate_model_name("../../../etc/passwd")


class TestValidateMethodName:
    """Test validate_method_name with happy path and adversarial inputs."""

    def test_valid_method_names(self):
        """Accept standard Odoo method names."""
        tool_helpers.validate_method_name("search")
        tool_helpers.validate_method_name("search_read")
        tool_helpers.validate_method_name("search_count")
        tool_helpers.validate_method_name("action_confirm")

    def test_valid_with_underscores(self):
        """Accept method names with underscores."""
        tool_helpers.validate_method_name("my_custom_action")
        tool_helpers.validate_method_name("_private_method")

    def test_invalid_empty_string(self):
        """Reject empty method names."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("")

    def test_invalid_starting_with_digit(self):
        """Reject method names starting with digit."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("123method")

    def test_invalid_with_dots(self):
        """Reject method names with dots."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("search.read")

    def test_invalid_with_spaces(self):
        """Reject method names with spaces."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("search read")

    def test_invalid_with_special_chars(self):
        """Reject method names with special characters."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("search@read")
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("search-read")

    def test_sql_injection_in_method(self):
        """Reject SQL injection patterns."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("action'; DROP--")

    def test_unicode_in_method_name(self):
        """Reject unicode characters."""
        with pytest.raises(ValueError, match="Invalid method name"):
            tool_helpers.validate_method_name("actión_confirmar")


class TestClampLimit:
    """Test clamp_limit for boundary conditions."""

    def test_limit_within_default_max(self):
        """Pass through limit within default max."""
        assert tool_helpers.clamp_limit(50) == 50
        assert tool_helpers.clamp_limit(1) == 1

    def test_limit_exceeds_default_max(self):
        """Clamp limit to default max."""
        assert tool_helpers.clamp_limit(200) == 100

    def test_limit_zero_raises(self):
        """Reject zero limit."""
        with pytest.raises(ValueError, match="greater than 0"):
            tool_helpers.clamp_limit(0)

    def test_negative_limit_raises(self):
        """Reject negative limit."""
        with pytest.raises(ValueError, match="greater than 0"):
            tool_helpers.clamp_limit(-10)

    def test_custom_maximum(self):
        """Use custom maximum when provided."""
        assert tool_helpers.clamp_limit(50, maximum=40) == 40
        assert tool_helpers.clamp_limit(30, maximum=40) == 30


class TestMaxSmartFields:
    """Test max_smart_fields env reading."""

    def test_default_when_unset(self):
        """Return default when env var unset."""
        result = tool_helpers.max_smart_fields()
        # Just verify it returns a positive int (exact default depends on agent_tools)
        assert isinstance(result, int)
        assert result > 0

    def test_env_var_numeric_value(self, monkeypatch):
        """Parse numeric env var."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "25")
        assert tool_helpers.max_smart_fields() == 25

    def test_env_var_with_whitespace(self, monkeypatch):
        """Strip whitespace from env var."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "  30  ")
        assert tool_helpers.max_smart_fields() == 30

    def test_env_var_zero_clamped_to_one(self, monkeypatch):
        """Clamp zero to 1."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "0")
        assert tool_helpers.max_smart_fields() == 1

    def test_env_var_negative_clamped_to_one(self, monkeypatch):
        """Clamp negative to 1."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "-5")
        assert tool_helpers.max_smart_fields() == 1

    def test_env_var_invalid_defaults(self, monkeypatch):
        """Return default on non-numeric value."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "not_a_number")
        result = tool_helpers.max_smart_fields()
        assert isinstance(result, int)
        assert result > 0

    def test_env_var_empty_string_defaults(self, monkeypatch):
        """Return default on empty string."""
        monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "")
        result = tool_helpers.max_smart_fields()
        assert isinstance(result, int)
        assert result > 0


class TestMaxAttachmentBytes:
    """Test max_attachment_bytes env reading."""

    def test_default_when_unset(self):
        """Return default (1 MiB) when unset."""
        # Monkeypatch first to ensure env var not set
        import os
        if "ODOO_MCP_MAX_ATTACHMENT_BYTES" in os.environ:
            del os.environ["ODOO_MCP_MAX_ATTACHMENT_BYTES"]
        assert tool_helpers.max_attachment_bytes() == 1024 * 1024

    def test_env_var_numeric_value(self, monkeypatch):
        """Parse numeric env var."""
        monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_BYTES", "2097152")  # 2 MiB
        assert tool_helpers.max_attachment_bytes() == 2097152

    def test_env_var_clamped_to_hard_cap(self, monkeypatch):
        """Clamp to 16 MiB hard cap."""
        monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_BYTES", "999999999")
        assert tool_helpers.max_attachment_bytes() == 16 * 1024 * 1024

    def test_env_var_zero_clamped_to_one(self, monkeypatch):
        """Clamp zero to 1."""
        monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_BYTES", "0")
        assert tool_helpers.max_attachment_bytes() == 1

    def test_env_var_invalid_defaults(self, monkeypatch):
        """Return default on non-numeric value."""
        monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_BYTES", "garbage")
        assert tool_helpers.max_attachment_bytes() == 1024 * 1024

    def test_env_var_with_whitespace(self, monkeypatch):
        """Strip whitespace from env var."""
        monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_BYTES", "  512000  ")
        assert tool_helpers.max_attachment_bytes() == 512000


class TestTruthyEnv:
    """Test truthy_env boolean flag parsing."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("yes", True),
            ("Yes", True),
            ("on", True),
            ("On", True),
            ("0", False),
            ("false", False),
            ("False", False),
            ("no", False),
            ("off", False),
            ("", False),
            ("maybe", False),
            ("nope", False),
        ],
    )
    def test_truthy_env_values(self, monkeypatch, value, expected):
        """Test various truthy/falsy env values."""
        if value:
            monkeypatch.setenv("TEST_FLAG", value)
        else:
            monkeypatch.delenv("TEST_FLAG", raising=False)
        assert tool_helpers.truthy_env("TEST_FLAG") is expected

    def test_truthy_env_with_whitespace(self, monkeypatch):
        """Strip whitespace before checking."""
        monkeypatch.setenv("TEST_FLAG", "  true  ")
        assert tool_helpers.truthy_env("TEST_FLAG") is True

    def test_truthy_env_unset(self):
        """Return False for unset env var."""
        import os
        if "NONEXISTENT_VAR" in os.environ:
            del os.environ["NONEXISTENT_VAR"]
        assert tool_helpers.truthy_env("NONEXISTENT_VAR") is False


class TestParseMeasureSpec:
    """Test parse_measure_spec for measure field:aggregator parsing."""

    def test_defaults_to_sum(self):
        """Default aggregator is 'sum' when no colon present."""
        field, agg = tool_helpers.parse_measure_spec("amount")
        assert field == "amount"
        assert agg == "sum"

    def test_explicit_aggregator(self):
        """Parse field:aggregator format."""
        field, agg = tool_helpers.parse_measure_spec("amount:avg")
        assert field == "amount"
        assert agg == "avg"

    def test_case_insensitive_aggregator(self):
        """Convert aggregator to lowercase."""
        field, agg = tool_helpers.parse_measure_spec("amount:AVG")
        assert field == "amount"
        assert agg == "avg"

    def test_whitespace_around_colon(self):
        """Trim whitespace from field and agg."""
        field, agg = tool_helpers.parse_measure_spec("  amount : avg  ")
        assert field == "amount"
        assert agg == "avg"

    @pytest.mark.parametrize(
        "spec",
        [
            "sum",
            "avg",
            "min",
            "max",
            "count",
            "count_distinct",
            "array_agg",
            "bool_and",
            "bool_or",
        ],
    )
    def test_all_valid_aggregators(self, spec):
        """Accept all supported aggregators."""
        field, agg = tool_helpers.parse_measure_spec(f"field:{spec}")
        assert agg == spec

    def test_empty_string_raises(self):
        """Reject empty spec."""
        with pytest.raises(ValueError, match="non-empty"):
            tool_helpers.parse_measure_spec("")

    def test_whitespace_only_raises(self):
        """Reject whitespace-only spec."""
        with pytest.raises(ValueError, match="non-empty"):
            tool_helpers.parse_measure_spec("   ")

    def test_empty_field_raises(self):
        """Reject empty field."""
        with pytest.raises(ValueError, match="invalid measure spec"):
            tool_helpers.parse_measure_spec(":avg")

    def test_empty_aggregator_raises(self):
        """Reject empty aggregator."""
        with pytest.raises(ValueError, match="invalid measure spec"):
            tool_helpers.parse_measure_spec("amount:")

    def test_unsupported_aggregator_raises(self):
        """Reject unsupported aggregator."""
        with pytest.raises(ValueError, match="unsupported aggregator"):
            tool_helpers.parse_measure_spec("amount:median")

    def test_multiple_colons_rejects_invalid_agg(self):
        """Multiple colons result in invalid aggregator (reject)."""
        with pytest.raises(ValueError, match="unsupported aggregator"):
            tool_helpers.parse_measure_spec("field:with:colons:avg")


class TestParseOdooMajorVersion:
    """Test parse_odoo_major_version version extraction."""

    def test_integer_input(self):
        """Extract from integer."""
        assert tool_helpers.parse_odoo_major_version(17) == 17
        assert tool_helpers.parse_odoo_major_version(1) == 1

    def test_integer_zero_returns_none(self):
        """Return None for zero or negative."""
        assert tool_helpers.parse_odoo_major_version(0) is None
        assert tool_helpers.parse_odoo_major_version(-1) is None

    def test_float_input(self):
        """Extract integer from float."""
        assert tool_helpers.parse_odoo_major_version(17.5) == 17
        assert tool_helpers.parse_odoo_major_version(1.0) == 1

    def test_float_less_than_one(self):
        """Return None for float < 1."""
        assert tool_helpers.parse_odoo_major_version(0.5) is None

    def test_string_input(self):
        """Extract first digits from string."""
        assert tool_helpers.parse_odoo_major_version("17") == 17
        assert tool_helpers.parse_odoo_major_version("17.0") == 17
        assert tool_helpers.parse_odoo_major_version("saas~19") == 19

    def test_string_no_digits(self):
        """Return None for string with no digits."""
        assert tool_helpers.parse_odoo_major_version("no_version") is None

    def test_string_empty(self):
        """Return None for empty string."""
        assert tool_helpers.parse_odoo_major_version("") is None

    def test_bool_input(self):
        """Return None for boolean."""
        assert tool_helpers.parse_odoo_major_version(True) is None
        assert tool_helpers.parse_odoo_major_version(False) is None

    def test_none_input(self):
        """Return None for None input."""
        assert tool_helpers.parse_odoo_major_version(None) is None


class TestNormalizeDomainInput:
    """Test normalize_domain_input for various domain formats."""

    def test_none_domain_returns_empty_list(self):
        """Convert None to empty domain."""
        assert tool_helpers.normalize_domain_input(None) == []

    def test_empty_list_returns_empty(self):
        """Convert empty list to empty domain."""
        assert tool_helpers.normalize_domain_input([]) == []
        assert tool_helpers.normalize_domain_input([[]]) == []
        assert tool_helpers.normalize_domain_input("") == []
        assert tool_helpers.normalize_domain_input("   ") == []

    def test_single_condition_list(self):
        """Convert single [field, op, value] condition."""
        result = tool_helpers.normalize_domain_input(["name", "=", "Ada"])
        assert result == [["name", "=", "Ada"]]

    def test_multiple_conditions_list(self):
        """Convert list of multiple conditions."""
        result = tool_helpers.normalize_domain_input(
            [["name", "=", "Ada"], ["age", ">", 18]]
        )
        assert result == [["name", "=", "Ada"], ["age", ">", 18]]

    def test_domain_with_operators(self):
        """Preserve domain operators."""
        result = tool_helpers.normalize_domain_input(
            ["&", ["name", "=", "Ada"], ["age", ">", 18]]
        )
        assert result == ["&", ["name", "=", "Ada"], ["age", ">", 18]]

    def test_json_string_domain(self):
        """Parse JSON string to domain."""
        json_str = '{"conditions": [{"field": "name", "operator": "=", "value": "Ada"}]}'
        result = tool_helpers.normalize_domain_input(json_str)
        assert result == [["name", "=", "Ada"]]

    def test_python_literal_string_domain(self):
        """Parse Python literal string to domain."""
        literal_str = "[['name', '=', 'Ada']]"
        result = tool_helpers.normalize_domain_input(literal_str)
        assert result == [["name", "=", "Ada"]]

    def test_python_tuple_literal_is_normalized(self):
        """Accept native Odoo tuple leaves instead of dropping them."""
        result = tool_helpers.normalize_domain_input('[("tag_ids", "in", [7])]')
        assert result == [["tag_ids", "in", [7]]]
        assert tool_helpers.normalize_domain_input(("name", "=", "Ada")) == [
            ["name", "=", "Ada"]
        ]

    def test_mixed_tuple_and_list_leaves_with_operators(self):
        """Keep prefix operators and convert tuple leaves in order."""
        result = tool_helpers.normalize_domain_input(
            ["|", ("name", "=", "Ada"), ["id", ">", 0]]
        )
        assert result == ["|", ["name", "=", "Ada"], ["id", ">", 0]]

    def test_invalid_json_string_raises(self):
        """Reject unparseable non-empty strings instead of broadening."""
        with pytest.raises(ValueError, match="domain must be"):
            tool_helpers.normalize_domain_input('{"broken": json}')

    def test_dict_with_conditions(self):
        """Parse dict with conditions key."""
        domain_dict = {
            "conditions": [
                {"field": "name", "operator": "=", "value": "Ada"},
                {"field": "age", "operator": ">", "value": 18},
            ]
        }
        result = tool_helpers.normalize_domain_input(domain_dict)
        assert result == [["name", "=", "Ada"], ["age", ">", 18]]

    def test_dict_missing_conditions_key(self):
        """Reject objects that are not the documented conditions shape."""
        with pytest.raises(ValueError, match="conditions"):
            tool_helpers.normalize_domain_input({"other": "data"})

    def test_dict_conditions_not_list(self):
        """Reject a conditions value that is not a list."""
        with pytest.raises(ValueError, match="conditions"):
            tool_helpers.normalize_domain_input({"conditions": "not_a_list"})

    def test_empty_conditions_list_returns_empty(self):
        """An explicit empty conditions list is an empty domain."""
        assert tool_helpers.normalize_domain_input({"conditions": []}) == []

    def test_incomplete_condition_entries_raise(self):
        """Do not silently drop incomplete structured conditions."""
        domain_dict = {
            "conditions": [
                {"field": "name", "operator": "=", "value": "Ada"},
                {"field": "age"},  # Missing operator and value
            ]
        }
        with pytest.raises(ValueError, match="field, operator, and value"):
            tool_helpers.normalize_domain_input(domain_dict)

    def test_nested_single_condition_list(self):
        """Unwrap nested single-condition lists."""
        result = tool_helpers.normalize_domain_input([["name", "=", "Ada"]])
        assert result == [["name", "=", "Ada"]]

    def test_non_list_non_dict_input(self):
        """Reject scalars and unparseable strings instead of broadening."""
        with pytest.raises(ValueError, match="domain must be"):
            tool_helpers.normalize_domain_input("string")
        with pytest.raises(ValueError, match="domain must be"):
            tool_helpers.normalize_domain_input(123)

    def test_invalid_conditions_raise_instead_of_dropping(self):
        """Leftover invalid terms must not silently shrink the domain."""
        with pytest.raises(ValueError, match="invalid domain term"):
            tool_helpers.normalize_domain_input(
                [
                    ["name", "=", "Ada"],
                    "invalid",  # Not a list
                    [1, 2, 3],  # Numbers instead of strings
                ]
            )

    def test_negation_operator(self):
        """Preserve negation operator."""
        result = tool_helpers.normalize_domain_input(
            ["!", ["name", "=", "Ada"]]
        )
        assert result == ["!", ["name", "=", "Ada"]]

    def test_or_operator(self):
        """Preserve OR operator."""
        result = tool_helpers.normalize_domain_input(
            ["|", ["name", "=", "Ada"], ["name", "=", "Grace"]]
        )
        assert result == ["|", ["name", "=", "Ada"], ["name", "=", "Grace"]]

    def test_sql_injection_in_domain_value(self):
        """Allow SQL-like strings in values (no validation here)."""
        # Domain validation doesn't reject SQL in values — that's Odoo's job
        result = tool_helpers.normalize_domain_input(
            ["name", "=", "'; DROP TABLE--"]
        )
        assert result == [["name", "=", "'; DROP TABLE--"]]


def test_normalize_group_order_rewrites_bare_measures_only():
    from odoo_mcp.tool_helpers import normalize_group_order

    measures = [("amount_untaxed", "sum"), ("id", "count")]
    assert normalize_group_order("amount_untaxed desc", measures) == "amount_untaxed:sum desc"
    assert normalize_group_order("amount_untaxed:avg desc", measures) == "amount_untaxed:avg desc"
    assert normalize_group_order("partner_id, __count desc", measures) == "partner_id, __count desc"
    assert normalize_group_order(" id ,  ", measures) == "id:count"

