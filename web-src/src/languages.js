// Small highlight.js grammars for languages common in specifications that
// highlight.js does not ship: CDDL (RFC 8610), Rego (OPA), ASN.1 and CBOR
// diagnostic notation (RFC 8949 §8).

function cddl(hljs) {
  return {
    name: "CDDL",
    aliases: ["cddl"],
    contains: [
      hljs.COMMENT(";", "$"),
      hljs.QUOTE_STRING_MODE,
      { className: "string", begin: /h'/, end: /'/ },
      { className: "type", begin: /\b(?:u?int|nint|uint|bstr|bytes|tstr|text|bool|true|false|nil|null|any|float(?:16|32|64)?(?:-(?:32|64))?|number|tdate|time|uri|uuid|regexp|undefined|eb64url|eb64legacy|eb16|encoded-cbor|biguint|bignint|bigint|integer|unsigned|decfrac|bigfloat|mime-message|cbor-any)\b/ },
      { className: "keyword", begin: /\.(?:size|bits|regexp|cbor|cborseq|within|and|lt|le|gt|ge|eq|ne|default)\b/ },
      { className: "title", begin: /^[A-Za-z@_$][\w@$.-]*(?=\s*\/{0,2}=)/m },
      { className: "symbol", begin: /#\d+(?:\.\d+)?/ },
      hljs.C_NUMBER_MODE,
      { className: "operator", begin: /\/\/=|\/=|=>|\.\.\.?|[?*+~&^]/ },
    ],
  };
}

function rego(hljs) {
  return {
    name: "Rego",
    aliases: ["rego"],
    keywords: {
      keyword: "package import as default else not some every in if contains with",
      literal: "true false null",
      built_in: "input data count sum max min sort startswith endswith contains concat sprintf time object array set union intersection",
    },
    contains: [
      hljs.HASH_COMMENT_MODE,
      hljs.QUOTE_STRING_MODE,
      { className: "string", begin: /`/, end: /`/ },
      hljs.C_NUMBER_MODE,
      { className: "title.function", begin: /^[a-z_][\w]*(?=\s*(?:\[|\(|:=|=|\{|if\b|contains\b))/m },
      { className: "operator", begin: /:=|==|!=|<=|>=|[<>|&]/ },
    ],
  };
}

function asn1(hljs) {
  return {
    name: "ASN.1",
    aliases: ["asn1", "asn"],
    keywords: {
      keyword:
        "BEGIN END DEFINITIONS IMPLICIT EXPLICIT TAGS AUTOMATIC IMPORTS EXPORTS FROM WITH COMPONENTS OF SIZE OPTIONAL DEFAULT CHOICE SEQUENCE SET ENUMERATED CLASS UNIQUE SYNTAX IDENTIFIED BY MIN MAX ALL EXCEPT INCLUDES APPLICATION PRIVATE UNIVERSAL CONTAINING ENCODED",
      type: "INTEGER BOOLEAN NULL REAL BIT STRING OCTET OBJECT IDENTIFIER UTF8String PrintableString IA5String UTCTime GeneralizedTime BMPString TeletexString UniversalString VisibleString NumericString ANY",
      literal: "TRUE FALSE",
    },
    contains: [hljs.COMMENT("--", "$"), hljs.QUOTE_STRING_MODE, hljs.C_NUMBER_MODE, { className: "symbol", begin: /\[\s*\d+\s*\]/ }, { className: "operator", begin: /::=/ }],
  };
}

function cbor(hljs) {
  return {
    name: "CBOR diagnostic",
    aliases: ["cbor", "cbor-diag", "edn"],
    contains: [
      hljs.C_LINE_COMMENT_MODE,
      hljs.C_BLOCK_COMMENT_MODE,
      hljs.COMMENT("#", "$"),
      hljs.QUOTE_STRING_MODE,
      { className: "string", begin: /(?:h|b64|b32|h32)'/, end: /'/ },
      { className: "symbol", begin: /\b\d+(?=\()/ },
      { className: "literal", begin: /\b(?:true|false|null|undefined|NaN|Infinity)\b/ },
      { className: "number", begin: /-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?(?:_\d)?\b/ },
    ],
  };
}

export function registerExtraLanguages(hljs) {
  hljs.registerLanguage("cddl", cddl);
  hljs.registerLanguage("rego", rego);
  hljs.registerLanguage("asn1", asn1);
  hljs.registerLanguage("cbor", cbor);
  hljs.registerAliases(["text", "txt", "plain", "plaintext"], { languageName: "plaintext" });
  hljs.registerAliases(["sh", "shell", "console", "zsh"], { languageName: "bash" });
}
