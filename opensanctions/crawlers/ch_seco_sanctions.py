from collections import defaultdict
from prefixdate import parse_parts

from opensanctions.core import Context
from opensanctions import helpers as h

# TODO: sanctions-program full parse

NAME_QUALITY_WEAK = {"good": False, "low": True}
NAME_TYPE = {
    "primary-name": "name",
    "alias": "alias",
    "formerly-known-as": "previousName",
}


def parse_address(node):
    address = {
        "remarks": node.findtext("./remarks"),
        "co": node.findtext("./c-o"),
        "location": node.findtext("./location"),
        "address-details": node.findtext("./address-details"),
        "p-o-box": node.findtext("./p-o-box"),
        "zip-code": node.findtext("./zip-code"),
        "area": node.findtext("./area"),
        "country": node.findtext("./country"),
    }
    return {k: v for (k, v) in address.items() if v is not None}


def compose_address(context: Context, entity, place, el):
    addr = dict(place)
    addr.update(parse_address(el))
    entity.add("country", addr.get("country"))
    po_box = addr.get("p-o-box")
    if po_box is not None:
        po_box = f"P.O. Box {po_box}"
    return h.make_address(
        context,
        remarks=addr.get("remarks"),
        summary=addr.get("co"),
        street=addr.get("address-details"),
        city=addr.get("location"),
        po_box=po_box,
        postal_code=addr.get("zip-code"),
        region=addr.get("area"),
        country=addr.get("country"),
    )


def parse_name(entity, node):
    name_prop = NAME_TYPE[node.get("name-type")]
    is_weak = NAME_QUALITY_WEAK[node.get("quality")]

    parts = defaultdict(dict)
    for part in node.findall("./name-part"):
        part_type = part.get("name-part-type")
        value = part.findtext("./value")
        parts[None][part_type] = value

        for spelling in part.findall("./spelling-variant"):
            key = (spelling.get("lang"), spelling.get("script"))
            parts[key][part_type] = spelling.text

    for key, parts in parts.items():
        entity.add("title", parts.pop("title", None), quiet=True)
        entity.add("title", parts.pop("suffix", None), quiet=True)
        entity.add("weakAlias", parts.pop("other", None), quiet=True)
        entity.add("weakAlias", parts.pop("tribal-name", None), quiet=True)
        entity.add("fatherName", parts.pop("grand-father-name", None), quiet=True)
        h.apply_name(
            entity,
            full=parts.pop("whole-name", None),
            given_name=parts.pop("given-name", None),
            second_name=parts.pop("further-given-name", None),
            patronymic=parts.pop("father-name", None),
            last_name=parts.pop("family-name", None),
            maiden_name=parts.pop("maiden-name", None),
            is_weak=is_weak,
            name_prop=name_prop,
            quiet=True,
        )
        h.audit_data(parts)


def parse_identity(context: Context, entity, node, places):
    for name in node.findall(".//name"):
        parse_name(entity, name)

    for address in node.findall(".//address"):
        place = places.get(address.get("place-id"))
        address = compose_address(context, entity, place, address)
        h.apply_address(context, entity, address)

    for bday in node.findall(".//day-month-year"):
        bval = parse_parts(bday.get("year"), bday.get("month"), bday.get("day"))
        if entity.schema.is_a("Person"):
            entity.add("birthDate", bval)
        else:
            entity.add("incorporationDate", bval)

    for nationality in node.findall(".//nationality"):
        country = nationality.find("./country")
        if country is not None:
            entity.add("nationality", country.get("iso-code"))
            entity.add("nationality", country.text)

    for bplace in node.findall(".//place-of-birth"):
        place = places.get(bplace.get("place-id"))
        address = compose_address(context, entity, place, bplace)
        entity.add("birthPlace", address.get("full"))

    for doc in node.findall(".//identification-document"):
        country = doc.find("./issuer")
        type_ = doc.get("document-type")
        number = doc.findtext("./number")
        entity.add("nationality", country.text, quiet=True)
        schema = "Identification"
        if type_ in ("id-card"):
            entity.add("idNumber", number)
        if type_ in ("passport", "diplomatic-passport"):
            entity.add("idNumber", number)
            schema = "Passport"
        passport = context.make(schema)
        passport.id = context.make_id(entity.id, type_, doc.get("ssid"))
        passport.add("holder", entity)
        passport.add("country", country.text)
        passport.add("number", number)
        passport.add("type", type_)
        passport.add("summary", doc.findtext("./remark"))
        passport.add("startDate", doc.findtext("./date-of-issue"))
        passport.add("endDate", doc.findtext("./expiry-date"))
        context.emit(passport)


def parse_entry(context: Context, target, programs, places, updated_at):
    entity = context.make("LegalEntity")
    node = target.find("./entity")
    if node is None:
        node = target.find("./individual")
        entity = context.make("Person")
    if node is None:
        node = target.find("./object")
        object_type = node.get("object-type")
        if object_type != "vessel":
            context.log.warning(
                "Unknown target type", target=target, object_type=object_type
            )
        entity = context.make("Vessel")

    entity.id = context.make_slug(target.get("ssid"))
    entity.add("gender", node.get("sex"), quiet=True)
    for other in node.findall("./other-information"):
        value = other.text.strip()
        if entity.schema.is_a("Vessel") and value.lower().startswith("imo"):
            _, imo_num = value.split(":", 1)
            entity.add("imoNumber", imo_num)
        else:
            entity.add("notes", h.clean_note(value))

    sanction = h.make_sanction(context, entity)
    dates = set()
    for mod in target.findall("./modification"):
        dates.add(mod.get("publication-date"))
        sanction.add("listingDate", mod.get("publication-date"))
        sanction.add("startDate", mod.get("effective-date"))
    dates_ = [d for d in dates if d is not None]
    if len(dates_):
        entity.add("createdAt", min(dates_))
        entity.add("modifiedAt", max(dates_))

    ssid = target.get("sanctions-set-id")
    sanction.add("program", programs.get(ssid))

    for justification in node.findall("./justification"):
        # TODO: should this go into sanction:reason?
        entity.add("notes", h.clean_note(justification.text))

    for relation in node.findall("./relation"):
        rel_type = relation.get("relation-type")
        target_id = context.make_slug(relation.get("target-id"))
        res = context.lookup("relations", rel_type)
        if res is None:
            context.log.warn(
                "Unknown relationship type",
                type=rel_type,
                source=entity,
                target=target_id,
            )
            continue

        rel = context.make(res.schema)
        rel.id = context.make_slug(relation.get("ssid"))
        rel.add(res.source, entity.id)
        rel.add(res.target, target_id)
        rel.add(res.text, rel_type)

        # rel_target = context.make(rel.schema.get(res.target).range)
        # rel_target.id = target_id
        # context.emit(rel_target)

        entity.add_schema(rel.schema.get(res.source).range)
        context.emit(rel)

    for identity in node.findall("./identity"):
        parse_identity(context, entity, identity, places)

    entity.add("topics", "sanction")
    context.emit(entity, target=True)
    context.emit(sanction)


def crawl(context: Context):
    path = context.fetch_resource("source.xml", context.dataset.data.url)
    context.export_resource(path, "text/xml", title=context.SOURCE_TITLE)
    doc = context.parse_resource_xml(path)
    updated_at = doc.getroot().get("date")

    programs = {}
    for sanc in doc.findall(".//sanctions-program"):
        ssid = sanc.find("./sanctions-set").get("ssid")
        programs[ssid] = sanc.findtext('./program-name[@lang="eng"]')

    places = {}
    for place in doc.findall(".//place"):
        places[place.get("ssid")] = parse_address(place)

    for target in doc.findall("./target"):
        parse_entry(context, target, programs, places, updated_at)
