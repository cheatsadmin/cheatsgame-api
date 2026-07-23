from cheatgame.product.models import Category, Feature, Product, ValuesList


def create_feature(*, name: str, feature_type: int, category: Category) -> Feature:
    feature, _ = Feature.objects.get_or_create(
        name=name.strip(),
        category=category,
        defaults={"feature_type": feature_type},
    )
    if feature.feature_type != feature_type:
        feature.feature_type = feature_type
        feature.save(update_fields=["feature_type"])
    return feature


def create_product_feature(*, value: str, product: Product, feature: Feature) -> ValuesList:
    clean_value = value.strip()
    existing_values = ValuesList.objects.filter(product=product, feature=feature).order_by("id")
    values_list = existing_values.first()

    if values_list is None:
        return ValuesList.objects.create(
            value=clean_value,
            product=product,
            feature=feature
        )

    values_list.value = clean_value
    values_list.save(update_fields=["value"])
    existing_values.exclude(id=values_list.id).delete()
    return values_list


def update_feature(*, feature_id: int, name: str, feature_type: int, category: Category) -> Feature:
    feature = Feature.objects.get(id=feature_id)
    feature.name = name
    feature.feature_type = feature_type
    feature.category = category
    feature.save()
    return feature


def delete_feature(*, feature_id: int) -> None:
    Feature.objects.get(id=feature_id).delete()


def update_product_feature(*, product_feature_id: int, value: str, product: Product, feature: Feature) -> ValuesList:
    values_list = ValuesList.objects.get(id=product_feature_id)
    values_list.value = value
    values_list.product = product
    values_list.feature = feature
    values_list.save()
    return values_list


def delete_product_feature(*, product_feature_id) -> None:
    ValuesList.objects.get(id=product_feature_id).delete()
